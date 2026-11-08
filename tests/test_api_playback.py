import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from media_manager.database import AsyncSessionLocal
from media_manager.models import IntegrationConfig, MediaItem, PlaybackStat
from media_manager.web.app import app
from media_manager.web.api.auth import get_current_user, get_admin_user

TEST_MOVIE_PATH = "/media/Movies/_ApiPlayback_Test_Movie_ (2020)/_ApiPlayback_Test_Movie_ (2020).mkv"
TEST_SHOW_EP1_PATH = "/media/TvShows/_ApiPlayback_Test_Show_/Season 1/1x01 - Pilot.mkv"
TEST_SHOW_EP2_PATH = "/media/TvShows/_ApiPlayback_Test_Show_/Season 1/1x02 - Second.mkv"
TEST_SOURCE_NAMES = ["plex", "emby", "jellyfin"]


async def _cleanup():
    async with AsyncSessionLocal() as db:
        paths = [TEST_MOVIE_PATH, TEST_SHOW_EP1_PATH, TEST_SHOW_EP2_PATH]
        item_ids_res = await db.execute(select(MediaItem.id).where(MediaItem.path.in_(paths)))
        item_ids = [row[0] for row in item_ids_res.all()]
        if item_ids:
            await db.execute(delete(PlaybackStat).where(PlaybackStat.media_item_id.in_(item_ids)))
        await db.execute(delete(MediaItem).where(MediaItem.path.in_(paths)))
        await db.execute(delete(IntegrationConfig).where(IntegrationConfig.service_name.in_(TEST_SOURCE_NAMES)))
        await db.commit()


def _seed_media_item(path, media_type, title, season=None, episode=None, file_created_at=None):
    async def run():
        async with AsyncSessionLocal() as db:
            item = MediaItem(
                path=path, storage_volume="/mnt", title=title, media_type=media_type,
                season=season, episode=episode,
                resolution="1080p", height=1080, width=1920,
                video_codec="h264", audio_codec="AAC", audio_channels=2,
                bitrate=5_000_000, size_bytes=1_000_000_000, quality_score=50,
                file_created_at=file_created_at or datetime.utcnow(),
            )
            db.add(item)
            await db.commit()
            await db.refresh(item)
            return item.id
    return asyncio.run(run())


def _seed_stat(media_item_id, source, play_count, last_played_at):
    async def run():
        async with AsyncSessionLocal() as db:
            db.add(PlaybackStat(
                media_item_id=media_item_id, source=source,
                play_count=play_count, last_played_at=last_played_at,
            ))
            await db.commit()
    asyncio.run(run())


class TestApiPlaybackBase(unittest.TestCase):
    def setUp(self):
        asyncio.run(_cleanup())
        admin_user = {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_admin_user] = lambda: admin_user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        asyncio.run(_cleanup())


class TestPlaybackConfigEndpoints(TestApiPlaybackBase):
    def test_get_config_empty_by_default(self):
        res = self.client.get("/api/v1/playback/config")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["configs"], [])

    def test_put_invalid_service_name_rejected(self):
        res = self.client.put("/api/v1/playback/config/radarr", json={"api_url": "http://x", "api_key": "k"})
        self.assertEqual(res.status_code, 400)

    def test_put_then_get_round_trips_including_path_mappings(self):
        res_put = self.client.put(
            "/api/v1/playback/config/plex",
            json={
                "api_url": "http://plex:32400",
                "api_key": "tok",
                "enabled": True,
                "path_mappings": [{"source_prefix": "/data", "local_prefix": "/mnt"}],
            },
        )
        self.assertEqual(res_put.status_code, 200)

        res_get = self.client.get("/api/v1/playback/config")
        configs = res_get.json()["configs"]
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["service_name"], "plex")
        self.assertEqual(configs[0]["api_url"], "http://plex:32400")
        self.assertEqual(configs[0]["path_mappings"], [{"source_prefix": "/data", "local_prefix": "/mnt"}])

    def test_put_without_path_mappings_clears_them(self):
        # The Setup form relies on this: it always re-sends the mappings it shows.
        self.client.put(
            "/api/v1/playback/config/emby",
            json={"api_url": "http://e", "api_key": "k",
                  "path_mappings": [{"source_prefix": "/data", "local_prefix": "/mnt"}]},
        )
        self.client.put("/api/v1/playback/config/emby", json={"api_url": "http://e", "api_key": "k"})

        configs = self.client.get("/api/v1/playback/config").json()["configs"]
        self.assertEqual(configs[0]["path_mappings"], [])

    def test_put_updates_existing_row(self):
        self.client.put("/api/v1/playback/config/plex", json={"api_url": "http://old", "api_key": "k1"})
        self.client.put("/api/v1/playback/config/plex", json={"api_url": "http://new", "api_key": "k2"})

        configs = self.client.get("/api/v1/playback/config").json()["configs"]
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["api_url"], "http://new")


class TestPlaybackTestConnectionEndpoint(TestApiPlaybackBase):
    def test_invalid_service_name_rejected(self):
        res = self.client.post("/api/v1/playback/test/radarr", json={"api_url": "http://x", "api_key": "k"})
        self.assertEqual(res.status_code, 400)

    def test_healthy_connection(self):
        with patch("media_manager.integrations.plex.PlexClient.get_status", new=AsyncMock(return_value=True)):
            res = self.client.post("/api/v1/playback/test/plex", json={"api_url": "http://plex", "api_key": "k"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"healthy": True, "service": "plex"})

    def test_unhealthy_connection(self):
        with patch("media_manager.integrations.jellyfin.JellyfinClient.get_status", new=AsyncMock(return_value=False)):
            res = self.client.post("/api/v1/playback/test/jellyfin", json={"api_url": "http://jf", "api_key": "k"})
        self.assertEqual(res.json()["healthy"], False)


class TestPlaybackSyncTriggerEndpoint(TestApiPlaybackBase):
    def test_triggers_background_sync(self):
        with patch("media_manager.web.api.playback.run_playback_sync", new=AsyncMock()) as mock_sync:
            res = self.client.post("/api/v1/playback/sync")
        self.assertEqual(res.status_code, 200)
        mock_sync.assert_called_once()


class TestPlaybackItemsEndpoint(TestApiPlaybackBase):
    def test_merges_across_sources_and_returns_breakdown(self):
        item_id = _seed_media_item(TEST_MOVIE_PATH, "movie", "_ApiPlayback_Test_Movie_")
        _seed_stat(item_id, "plex", 2, datetime(2026, 1, 1))
        _seed_stat(item_id, "jellyfin", 5, datetime(2026, 3, 1))

        res = self.client.get("/api/v1/playback/items", params={"media_type": "movie"})
        self.assertEqual(res.status_code, 200)
        items = [i for i in res.json()["items"] if i["media_item_id"] == item_id]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["play_count"], 5)  # max across sources, not summed
        self.assertTrue(item["last_played_at"].startswith("2026-03-01"))
        self.assertEqual(len(item["sources"]), 2)

    def test_unplayed_only_filter(self):
        played_id = _seed_media_item(TEST_MOVIE_PATH, "movie", "_ApiPlayback_Test_Movie_")
        _seed_stat(played_id, "plex", 1, datetime(2026, 1, 1))

        res = self.client.get("/api/v1/playback/items", params={"media_type": "movie", "unplayed_only": True})
        item_ids = [i["media_item_id"] for i in res.json()["items"]]
        self.assertNotIn(played_id, item_ids)

    def test_min_days_since_played_filter(self):
        item_id = _seed_media_item(TEST_MOVIE_PATH, "movie", "_ApiPlayback_Test_Movie_")
        _seed_stat(item_id, "plex", 1, datetime.utcnow() - timedelta(days=1))

        res = self.client.get(
            "/api/v1/playback/items", params={"media_type": "movie", "min_days_since_played": 30}
        )
        item_ids = [i["media_item_id"] for i in res.json()["items"]]
        self.assertNotIn(item_id, item_ids)

    def test_never_played_item_included_with_zero_count(self):
        item_id = _seed_media_item(TEST_MOVIE_PATH, "movie", "_ApiPlayback_Test_Movie_")

        res = self.client.get("/api/v1/playback/items", params={"media_type": "movie"})
        items = [i for i in res.json()["items"] if i["media_item_id"] == item_id]
        self.assertEqual(items[0]["play_count"], 0)
        self.assertIsNone(items[0]["last_played_at"])

    def test_items_include_added_at(self):
        added = datetime(2024, 5, 1)
        item_id = _seed_media_item(TEST_MOVIE_PATH, "movie", "_ApiPlayback_Test_Movie_", file_created_at=added)

        res = self.client.get("/api/v1/playback/items", params={"media_type": "movie"})
        items = [i for i in res.json()["items"] if i["media_item_id"] == item_id]
        self.assertTrue(items[0]["added_at"].startswith("2024-05-01"))


class TestPlaybackSeriesEndpoint(TestApiPlaybackBase):
    def test_rolls_up_episodes_into_series(self):
        ep1_id = _seed_media_item(TEST_SHOW_EP1_PATH, "tv", "1x01 - Pilot", season=1, episode=1)
        ep2_id = _seed_media_item(TEST_SHOW_EP2_PATH, "tv", "1x02 - Second", season=1, episode=2)
        _seed_stat(ep1_id, "plex", 2, datetime(2026, 1, 1))
        _seed_stat(ep2_id, "plex", 0, None)

        res = self.client.get("/api/v1/playback/series")
        self.assertEqual(res.status_code, 200)
        series = [s for s in res.json()["series"] if s["show_name"] == "_ApiPlayback_Test_Show_"]
        self.assertEqual(len(series), 1)
        entry = series[0]
        self.assertEqual(entry["episode_count"], 2)
        self.assertEqual(entry["total_plays"], 2)
        self.assertEqual(entry["never_played_episode_count"], 1)
        self.assertTrue(entry["last_played_at"].startswith("2026-01-01"))

    def test_min_days_since_played_filters_series(self):
        ep1_id = _seed_media_item(TEST_SHOW_EP1_PATH, "tv", "1x01 - Pilot", season=1, episode=1)
        _seed_stat(ep1_id, "plex", 1, datetime.utcnow() - timedelta(days=1))

        res = self.client.get("/api/v1/playback/series", params={"min_days_since_played": 30})
        names = [s["show_name"] for s in res.json()["series"]]
        self.assertNotIn("_ApiPlayback_Test_Show_", names)

    def test_series_added_at_is_earliest_episode(self):
        _seed_media_item(
            TEST_SHOW_EP1_PATH, "tv", "1x01 - Pilot", season=1, episode=1,
            file_created_at=datetime(2026, 1, 1),
        )
        _seed_media_item(
            TEST_SHOW_EP2_PATH, "tv", "1x02 - Second", season=1, episode=2,
            file_created_at=datetime(2024, 6, 1),
        )

        res = self.client.get("/api/v1/playback/series")
        series = [s for s in res.json()["series"] if s["show_name"] == "_ApiPlayback_Test_Show_"]
        self.assertTrue(series[0]["added_at"].startswith("2024-06-01"))

    def test_series_sources_is_union_of_episode_sources_with_plays(self):
        ep1_id = _seed_media_item(TEST_SHOW_EP1_PATH, "tv", "1x01 - Pilot", season=1, episode=1)
        ep2_id = _seed_media_item(TEST_SHOW_EP2_PATH, "tv", "1x02 - Second", season=1, episode=2)
        _seed_stat(ep1_id, "plex", 2, datetime(2026, 1, 1))
        _seed_stat(ep2_id, "jellyfin", 1, datetime(2026, 1, 2))
        _seed_stat(ep2_id, "emby", 0, None)

        res = self.client.get("/api/v1/playback/series")
        series = [s for s in res.json()["series"] if s["show_name"] == "_ApiPlayback_Test_Show_"]
        self.assertEqual(series[0]["sources"], ["jellyfin", "plex"])


if __name__ == "__main__":
    unittest.main()
