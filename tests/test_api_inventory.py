import asyncio
import unittest
from datetime import datetime
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from media_manager.web.app import app
from media_manager.database import AsyncSessionLocal
from media_manager.models import MediaItem, PlaybackStat

TEST_MOVIE_PATH = "/media/Movies/_InventoryPlayback_Test_Movie_ (2020)/_InventoryPlayback_Test_Movie_ (2020).mkv"


class TestApiInventory(unittest.TestCase):
    def setUp(self):
        from media_manager.web.api.auth import get_current_user, get_admin_user
        admin_user = {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_admin_user] = lambda: admin_user
        self.client = TestClient(app)
        asyncio.run(self._cleanup_playback_fixture())

    def tearDown(self):
        app.dependency_overrides.clear()
        asyncio.run(self._cleanup_playback_fixture())

    async def _cleanup_playback_fixture(self):
        async with AsyncSessionLocal() as db:
            item_ids_res = await db.execute(select(MediaItem.id).where(MediaItem.path == TEST_MOVIE_PATH))
            item_ids = [row[0] for row in item_ids_res.all()]
            if item_ids:
                await db.execute(delete(PlaybackStat).where(PlaybackStat.media_item_id.in_(item_ids)))
            await db.execute(delete(MediaItem).where(MediaItem.path == TEST_MOVIE_PATH))
            await db.commit()

    def test_inventory_items_endpoint(self):
        response = self.client.get("/api/v1/inventory/items")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("total", data)
        self.assertIn("items", data)
        self.assertIsInstance(data["items"], list)

    def test_inventory_items_returns_200_when_enrichment_fails(self):
        from unittest.mock import AsyncMock
        with patch(
            "media_manager.web.api.integrations._populate_media_details",
            new=AsyncMock(side_effect=RuntimeError("enrichment boom")),
        ):
            response = self.client.get("/api/v1/inventory/items", params={"media_type": "movie"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("items", response.json())

    def test_inventory_items_include_playback_stats(self):
        async def seed():
            async with AsyncSessionLocal() as db:
                item = MediaItem(
                    path=TEST_MOVIE_PATH, storage_volume="/media/Movies",
                    title="_InventoryPlayback_Test_Movie_", media_type="movie",
                    resolution="1080p", height=1080, width=1920,
                    video_codec="h264", audio_codec="AAC", audio_channels=2,
                    bitrate=5_000_000, size_bytes=1_000_000_000, quality_score=50,
                    file_created_at=datetime.utcnow(),
                )
                db.add(item)
                await db.commit()
                await db.refresh(item)
                db.add(PlaybackStat(
                    media_item_id=item.id, source="plex", play_count=4,
                    last_played_at=datetime(2026, 4, 1),
                ))
                await db.commit()
                return item.id
        media_item_id = asyncio.run(seed())

        response = self.client.get("/api/v1/inventory/items", params={"q": "_InventoryPlayback_Test_Movie_"})
        self.assertEqual(response.status_code, 200)
        items = [i for i in response.json()["items"] if i["id"] == media_item_id]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["play_count"], 4)
        self.assertEqual(items[0]["play_sources"], ["plex"])
        self.assertTrue(items[0]["last_played_at"].startswith("2026-04-01"))

    def test_inventory_items_returns_200_when_playback_enrichment_fails(self):
        from unittest.mock import AsyncMock
        with patch(
            "media_manager.web.services.playback_lookup.load_stats_by_media_item",
            new=AsyncMock(side_effect=RuntimeError("playback lookup boom")),
        ):
            response = self.client.get("/api/v1/inventory/items", params={"media_type": "movie"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("items", response.json())

    def test_duplicates_endpoint(self):
        response = self.client.get("/api/v1/inventory/duplicates")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("groups", data)
        self.assertIsInstance(data["groups"], list)

    def test_scan_trigger_endpoint(self):
        # scan_all() walks the real configured MEDIA_DIRECTORIES and shells
        # out to ffprobe per file - on a real homelab checkout that's the
        # live media library, so it must be mocked out here rather than
        # actually executed. FastAPI's BackgroundTasks runs synchronously
        # within TestClient's request/response cycle, so without this patch
        # the real scan would run inline before the response returns.
        with patch("media_manager.web.api.inventory.scan_all") as mock_scan_all:
            response = self.client.post("/api/v1/inventory/scan")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data.get("status"), "scan_initiated")
            mock_scan_all.assert_called_once()

    def test_directories_crud(self):
        # 1. List (should return default config list if empty)
        res_list = self.client.get("/api/v1/integrations/directories")
        self.assertEqual(res_list.status_code, 200)
        dirs = res_list.json()
        self.assertGreater(len(dirs), 0)

        # 2. Add a directory
        test_path = "/mnt/TestDirectoryMonitored"
        res_add = self.client.post("/api/v1/integrations/directories", json={"path": test_path})
        self.assertEqual(res_add.status_code, 200)
        add_data = res_add.json()
        self.assertTrue(add_data["success"])
        dir_id = add_data["id"]

        # 3. List again (should contain the added one)
        res_list2 = self.client.get("/api/v1/integrations/directories")
        self.assertEqual(res_list2.status_code, 200)
        dirs2 = res_list2.json()
        paths = [d["path"] for d in dirs2]
        self.assertIn(test_path, paths)

        # 4. Remove it
        res_del = self.client.delete(f"/api/v1/integrations/directories/{dir_id}")
        self.assertEqual(res_del.status_code, 200)
        self.assertTrue(res_del.json()["success"])

if __name__ == "__main__":
    unittest.main()
