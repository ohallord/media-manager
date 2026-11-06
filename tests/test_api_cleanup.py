import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from media_manager.database import AsyncSessionLocal
from media_manager.models import CleanupSettings, MediaItem, PlaybackStat
from media_manager.web.app import app
from media_manager.web.api.auth import get_current_user, get_admin_user
from media_manager.web.api import cleanup as cleanup_module

OLD = datetime.utcnow() - timedelta(days=400)
TEST_MOVIE_PATH = "/media/Movies/_ApiCleanup_Test_Movie_ (2020)/_ApiCleanup_Test_Movie_ (2020).mkv"

# Tracks every MediaItem id seeded by _seed_movie() across the test run, so
# _cleanup() can still remove its PlaybackStat row even after a test (e.g.
# the delete-endpoint test) has already deleted the MediaItem row itself -
# SQLite doesn't enforce the model's ON DELETE CASCADE by default, so that
# row would otherwise be orphaned and leak into unrelated tests that assert
# on the full playback_stats table.
_SEEDED_ITEM_IDS: list = []


async def _cleanup():
    async with AsyncSessionLocal() as db:
        item_ids_res = await db.execute(select(MediaItem.id).where(MediaItem.path == TEST_MOVIE_PATH))
        item_ids = set(row[0] for row in item_ids_res.all()) | set(_SEEDED_ITEM_IDS)
        if item_ids:
            await db.execute(delete(PlaybackStat).where(PlaybackStat.media_item_id.in_(item_ids)))
        await db.execute(delete(MediaItem).where(MediaItem.path == TEST_MOVIE_PATH))
        await db.execute(delete(CleanupSettings).where(CleanupSettings.media_type.in_(["movie", "tv"])))
        await db.commit()
    _SEEDED_ITEM_IDS.clear()


def _seed_movie(file_created_at=None):
    async def run():
        async with AsyncSessionLocal() as db:
            item = MediaItem(
                path=TEST_MOVIE_PATH, storage_volume="/media/Movies", title="_ApiCleanup_Test_Movie_",
                media_type="movie", resolution="1080p", height=1080, width=1920,
                video_codec="h264", audio_codec="AAC", audio_channels=2,
                bitrate=5_000_000, size_bytes=1_000_000, quality_score=50,
                file_created_at=file_created_at or OLD,
            )
            db.add(item)
            await db.commit()
            await db.refresh(item)
            db.add(PlaybackStat(media_item_id=item.id, source="plex", play_count=0, last_played_at=None))
            await db.commit()
            _SEEDED_ITEM_IDS.append(item.id)
            return item.id
    return asyncio.run(run())


class TestApiCleanupBase(unittest.TestCase):
    def setUp(self):
        asyncio.run(_cleanup())
        cleanup_module._clear_suggestions_cache()
        self.admin_user = {"username": "admin", "role": "admin"}
        self.regular_user = {"username": "bob", "role": "user"}
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        app.dependency_overrides[get_admin_user] = lambda: self.admin_user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        asyncio.run(_cleanup())
        cleanup_module._clear_suggestions_cache()


class TestCleanupSettingsEndpoints(TestApiCleanupBase):
    def test_get_settings_returns_defaults(self):
        res = self.client.get("/api/v1/cleanup/settings")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["movie"]["never_played_grace_days"], 180)
        self.assertEqual(data["movie"]["stale_after_days"], 365)
        self.assertEqual(data["tv"]["never_played_grace_days"], 180)

    def test_put_updates_settings(self):
        res = self.client.put(
            "/api/v1/cleanup/settings/movie",
            json={"never_played_grace_days": 30, "stale_after_days": 90},
        )
        self.assertEqual(res.status_code, 200)

        data = self.client.get("/api/v1/cleanup/settings").json()
        self.assertEqual(data["movie"]["never_played_grace_days"], 30)
        self.assertEqual(data["movie"]["stale_after_days"], 90)

    def test_put_invalid_media_type_rejected(self):
        res = self.client.put(
            "/api/v1/cleanup/settings/bogus",
            json={"never_played_grace_days": 30, "stale_after_days": 90},
        )
        self.assertEqual(res.status_code, 400)

    def test_put_negative_values_rejected(self):
        res = self.client.put(
            "/api/v1/cleanup/settings/movie",
            json={"never_played_grace_days": -1, "stale_after_days": 90},
        )
        self.assertEqual(res.status_code, 400)

    def test_put_requires_admin(self):
        def _deny():
            raise HTTPException(status_code=403, detail="Admin access required")
        app.dependency_overrides[get_admin_user] = _deny
        res = self.client.put(
            "/api/v1/cleanup/settings/movie",
            json={"never_played_grace_days": 30, "stale_after_days": 90},
        )
        self.assertEqual(res.status_code, 403)


class TestCleanupSuggestionsEndpoint(TestApiCleanupBase):
    def test_suggestions_include_seeded_stale_movie(self):
        item_id = _seed_movie()
        res = self.client.get("/api/v1/cleanup/suggestions")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        ids = [s["media_item_id"] for s in data["movie"]["suggestions"]]
        self.assertIn(item_id, ids)
        self.assertIn("tv", data)
        self.assertFalse(data["cached"])

    def test_second_call_is_cached(self):
        _seed_movie()
        first = self.client.get("/api/v1/cleanup/suggestions").json()
        self.assertFalse(first["cached"])
        second = self.client.get("/api/v1/cleanup/suggestions").json()
        self.assertTrue(second["cached"])

    def test_force_refresh_bypasses_cache(self):
        _seed_movie()
        self.client.get("/api/v1/cleanup/suggestions")
        res = self.client.get("/api/v1/cleanup/suggestions", params={"force_refresh": True})
        self.assertFalse(res.json()["cached"])


class TestCleanupDeleteEndpoint(TestApiCleanupBase):
    def test_delete_requires_admin(self):
        def _deny():
            raise HTTPException(status_code=403, detail="Admin access required")
        app.dependency_overrides[get_admin_user] = _deny
        res = self.client.post("/api/v1/cleanup/delete", json={"media_item_ids": ["x"]})
        self.assertEqual(res.status_code, 403)

    def test_empty_selection_is_a_no_op(self):
        res = self.client.post("/api/v1/cleanup/delete", json={"media_item_ids": []})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["deleted_count"], 0)

    def test_unknown_ids_are_a_no_op(self):
        res = self.client.post("/api/v1/cleanup/delete", json={"media_item_ids": ["does-not-exist"]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["deleted_count"], 0)

    def test_blocked_when_volume_unhealthy(self):
        item_id = _seed_movie()
        with patch("media_manager.web.api.cleanup.volume_is_healthy", return_value=False):
            res = self.client.post("/api/v1/cleanup/delete", json={"media_item_ids": [item_id]})
        self.assertEqual(res.status_code, 409)

    def test_deletes_file_and_db_row(self):
        item_id = _seed_movie()
        with patch("media_manager.web.api.cleanup.volume_is_healthy", return_value=True), \
             patch("os.path.exists", return_value=True), \
             patch("os.remove") as mock_remove:
            res = self.client.post("/api/v1/cleanup/delete", json={"media_item_ids": [item_id]})

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["deleted_count"], 1)
        self.assertEqual(data["freed_bytes"], 1_000_000)
        mock_remove.assert_called_once()

        async def check_gone():
            async with AsyncSessionLocal() as db:
                res = await db.execute(select(MediaItem).where(MediaItem.id == item_id))
                self.assertIsNone(res.scalar_one_or_none())
        asyncio.run(check_gone())


if __name__ == "__main__":
    unittest.main()
