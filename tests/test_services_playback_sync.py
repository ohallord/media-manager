import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete, select

from media_manager.database import AsyncSessionLocal
from media_manager.models import AppLogEvent, IntegrationConfig, MediaItem, PlaybackStat
from media_manager.web.services.playback_sync import run_playback_sync, playback_sync_scheduler

TEST_MOVIE_PATH = "/media/Movies/_Playback_Test_Movie_ (2020)/_Playback_Test_Movie_ (2020).mkv"
TEST_SOURCE_NAMES = ["plex", "emby", "jellyfin"]


class _PlaybackSyncTestBase(unittest.TestCase):
    def setUp(self):
        asyncio.run(self._cleanup())

    def tearDown(self):
        asyncio.run(self._cleanup())

    async def _cleanup(self):
        async with AsyncSessionLocal() as db:
            item_ids_res = await db.execute(select(MediaItem.id).where(MediaItem.path == TEST_MOVIE_PATH))
            item_ids = [row[0] for row in item_ids_res.all()]
            if item_ids:
                await db.execute(delete(PlaybackStat).where(PlaybackStat.media_item_id.in_(item_ids)))
            await db.execute(delete(MediaItem).where(MediaItem.path == TEST_MOVIE_PATH))
            await db.execute(delete(IntegrationConfig).where(IntegrationConfig.service_name.in_(TEST_SOURCE_NAMES)))
            await db.execute(delete(AppLogEvent).where(AppLogEvent.event_tag == "[PLAYBACK_SYNC]"))
            await db.commit()

    async def _seed_media_item(self) -> str:
        async with AsyncSessionLocal() as db:
            item = MediaItem(
                path=TEST_MOVIE_PATH, storage_volume="/media/Movies",
                title="_Playback_Test_Movie_", media_type="movie",
                resolution="1080p", height=1080, width=1920,
                video_codec="h264", audio_codec="AAC", audio_channels=2,
                bitrate=5_000_000, size_bytes=1_000_000_000, quality_score=50,
                file_created_at=datetime.utcnow(),
            )
            db.add(item)
            await db.commit()
            await db.refresh(item)
            return item.id

    async def _seed_config(self, service_name: str, enabled: bool = True, api_key: str = "tok") -> None:
        async with AsyncSessionLocal() as db:
            db.add(IntegrationConfig(
                service_name=service_name, api_url="http://source", api_key_encrypted=api_key,
                enabled=enabled, status="UNKNOWN",
            ))
            await db.commit()


class TestRunPlaybackSync(_PlaybackSyncTestBase):
    def test_no_configs_returns_empty_summary(self):
        async def run():
            summary = await run_playback_sync()
            self.assertEqual(summary, {})
        asyncio.run(run())

    def test_matches_and_upserts_stat(self):
        async def run():
            media_item_id = await self._seed_media_item()
            await self._seed_config("plex")

            remote_items = [{
                "path": TEST_MOVIE_PATH, "external_id": "999", "play_count": 3,
                "last_played_at": datetime(2026, 1, 1), "media_type": "movie",
                "series_name": None, "season": None, "episode": None,
            }]
            with patch(
                "media_manager.integrations.plex.PlexClient.get_library_items",
                new=AsyncMock(return_value=remote_items),
            ):
                summary = await run_playback_sync()

            self.assertEqual(summary["plex"], {"matched": 1, "unmatched": 0, "error": False})
            self.assertNotIn("emby", summary)
            self.assertNotIn("jellyfin", summary)

            async with AsyncSessionLocal() as db:
                stat_res = await db.execute(
                    select(PlaybackStat).where(
                        PlaybackStat.media_item_id == media_item_id, PlaybackStat.source == "plex"
                    )
                )
                stat = stat_res.scalar_one()
                self.assertEqual(stat.play_count, 3)
                self.assertEqual(stat.last_played_at, datetime(2026, 1, 1))
                self.assertEqual(stat.source_item_id, "999")

                config_res = await db.execute(
                    select(IntegrationConfig).where(IntegrationConfig.service_name == "plex")
                )
                config = config_res.scalar_one()
                self.assertEqual(config.status, "HEALTHY")
                self.assertIsNotNone(config.last_checked_at)

                event_res = await db.execute(select(AppLogEvent).where(AppLogEvent.event_tag == "[PLAYBACK_SYNC]"))
                self.assertIsNotNone(event_res.scalars().first())
        asyncio.run(run())

    def test_second_sync_updates_existing_stat_instead_of_duplicating(self):
        async def run():
            media_item_id = await self._seed_media_item()
            await self._seed_config("plex")

            first_items = [{
                "path": TEST_MOVIE_PATH, "external_id": "1", "play_count": 1,
                "last_played_at": datetime(2026, 1, 1), "media_type": "movie",
                "series_name": None, "season": None, "episode": None,
            }]
            second_items = [{
                "path": TEST_MOVIE_PATH, "external_id": "1", "play_count": 5,
                "last_played_at": datetime(2026, 2, 1), "media_type": "movie",
                "series_name": None, "season": None, "episode": None,
            }]
            with patch(
                "media_manager.integrations.plex.PlexClient.get_library_items",
                new=AsyncMock(side_effect=[first_items, second_items]),
            ):
                await run_playback_sync()
                await run_playback_sync()

            async with AsyncSessionLocal() as db:
                stats_res = await db.execute(
                    select(PlaybackStat).where(
                        PlaybackStat.media_item_id == media_item_id, PlaybackStat.source == "plex"
                    )
                )
                stats = stats_res.scalars().all()
                self.assertEqual(len(stats), 1)
                self.assertEqual(stats[0].play_count, 5)
        asyncio.run(run())

    def test_unmatched_item_not_stored(self):
        async def run():
            await self._seed_media_item()
            await self._seed_config("plex")

            remote_items = [{
                "path": "/completely/different/path/movie.mkv", "external_id": "1", "play_count": 2,
                "last_played_at": None, "media_type": "movie",
                "series_name": None, "season": None, "episode": None,
            }]
            with patch(
                "media_manager.integrations.plex.PlexClient.get_library_items",
                new=AsyncMock(return_value=remote_items),
            ):
                summary = await run_playback_sync()

            self.assertEqual(summary["plex"], {"matched": 0, "unmatched": 1, "error": False})

            async with AsyncSessionLocal() as db:
                stat_res = await db.execute(select(PlaybackStat))
                self.assertEqual(stat_res.scalars().all(), [])
        asyncio.run(run())

    def test_fetch_exception_marks_source_error(self):
        async def run():
            await self._seed_media_item()
            await self._seed_config("plex")

            with patch(
                "media_manager.integrations.plex.PlexClient.get_library_items",
                new=AsyncMock(side_effect=RuntimeError("plex down")),
            ):
                summary = await run_playback_sync()

            self.assertEqual(summary["plex"], {"matched": 0, "unmatched": 0, "error": True})

            async with AsyncSessionLocal() as db:
                config_res = await db.execute(
                    select(IntegrationConfig).where(IntegrationConfig.service_name == "plex")
                )
                self.assertEqual(config_res.scalar_one().status, "ERROR")
        asyncio.run(run())

    def test_disabled_source_skipped(self):
        async def run():
            await self._seed_media_item()
            await self._seed_config("plex", enabled=False)

            with patch(
                "media_manager.integrations.plex.PlexClient.get_library_items", new=AsyncMock()
            ) as mock_get_items:
                summary = await run_playback_sync()
                mock_get_items.assert_not_called()

            self.assertNotIn("plex", summary)
        asyncio.run(run())

    def test_missing_api_key_skipped(self):
        async def run():
            await self._seed_media_item()
            await self._seed_config("plex", api_key="")

            with patch(
                "media_manager.integrations.plex.PlexClient.get_library_items", new=AsyncMock()
            ) as mock_get_items:
                summary = await run_playback_sync()
                mock_get_items.assert_not_called()

            self.assertNotIn("plex", summary)
        asyncio.run(run())

    def test_top_level_exception_is_caught(self):
        async def run():
            with patch("media_manager.web.services.playback_sync.AsyncSessionLocal", side_effect=RuntimeError("db exploded")):
                summary = await run_playback_sync()
                self.assertEqual(summary, {})
        asyncio.run(run())


class TestPlaybackSyncScheduler(unittest.TestCase):
    def test_runs_initial_delay_then_sync_then_loops(self):
        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 2:
                raise asyncio.CancelledError()

        async def run():
            with patch("media_manager.web.services.playback_sync.run_playback_sync", new=AsyncMock()) as mock_run, \
                 patch("asyncio.sleep", new=fake_sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await playback_sync_scheduler()
                mock_run.assert_called_once()
                self.assertEqual(sleep_calls, [300, 6 * 3600])
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
