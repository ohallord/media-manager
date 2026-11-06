import unittest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock
from sqlalchemy import delete

from media_manager.database import AsyncSessionLocal
from media_manager.models import MediaItem, MetadataCache, MovieCastCache
from media_manager.web.services.metadata_sync import (
    run_metadata_sync, weekly_metadata_sync_scheduler
)

SEEDED_PATHS = [
    "/media/Movies/_Sync_Test_Movie_ (2020)/_Sync_Test_Movie_ (2020).mkv",
    "/media/TvShows/_Sync_Test_Show_/Season 1/1x01 - Pilot.mkv",
]


class TestRunMetadataSync(unittest.TestCase):
    def setUp(self):
        asyncio.run(self._cleanup())

    def tearDown(self):
        asyncio.run(self._cleanup())

    async def _cleanup(self):
        async with AsyncSessionLocal() as db:
            await db.execute(delete(MediaItem).where(MediaItem.path.in_(SEEDED_PATHS)))
            await db.execute(delete(MetadataCache).where(MetadataCache.cache_key.in_(
                ["movie:_sync_test_movie_", "tv:_sync_test_show_"]
            )))
            await db.commit()

    def test_no_media_items_returns_early(self):
        async def run():
            with patch("media_manager.web.services.metadata_sync._populate_media_details", new=AsyncMock()) as mock_populate:
                await run_metadata_sync()
                mock_populate.assert_not_called()
        asyncio.run(run())

    def test_syncs_uncached_items(self):
        async def run():
            async with AsyncSessionLocal() as db:
                db.add(MediaItem(
                    path=SEEDED_PATHS[0], storage_volume="/media/Movies",
                    title="_Sync_Test_Movie_", media_type="movie",
                    resolution="1080p", height=1080, width=1920,
                    video_codec="h264", audio_codec="AAC", audio_channels=2,
                    bitrate=5_000_000, size_bytes=1_000_000_000, quality_score=50,
                    file_created_at=datetime.utcnow(),
                ))
                await db.commit()

            with patch("media_manager.web.services.metadata_sync._populate_media_details", new=AsyncMock()) as mock_populate, \
                 patch("asyncio.sleep", new=AsyncMock()):
                await run_metadata_sync()
                mock_populate.assert_called_once()
                chunk_arg = mock_populate.call_args[0][0]
                self.assertEqual(len(chunk_arg), 1)
                self.assertEqual(chunk_arg[0]["title"], "_Sync_Test_Movie_")
                self.assertEqual(chunk_arg[0]["media_type"], "movie")
        asyncio.run(run())

    def test_skips_recently_cached_items_with_cast(self):
        async def run():
            async with AsyncSessionLocal() as db:
                db.add(MediaItem(
                    path=SEEDED_PATHS[0], storage_volume="/media/Movies",
                    title="_Sync_Test_Movie_", media_type="movie",
                    resolution="1080p", height=1080, width=1920,
                    video_codec="h264", audio_codec="AAC", audio_channels=2,
                    bitrate=5_000_000, size_bytes=1_000_000_000, quality_score=50,
                    file_created_at=datetime.utcnow(),
                ))
                db.add(MetadataCache(
                    cache_key="movie:_sync_test_movie_",
                    title="_Sync_Test_Movie_", media_type="movie",
                    tmdb_id=999999, updated_at=datetime.utcnow(),
                ))
                db.add(MovieCastCache(tmdb_id=999999, cast_json="[]"))
                await db.commit()

            with patch("media_manager.web.services.metadata_sync._populate_media_details", new=AsyncMock()) as mock_populate:
                await run_metadata_sync()
                mock_populate.assert_not_called()
        asyncio.run(run())

    def test_resyncs_stale_cache(self):
        async def run():
            async with AsyncSessionLocal() as db:
                db.add(MediaItem(
                    path=SEEDED_PATHS[0], storage_volume="/media/Movies",
                    title="_Sync_Test_Movie_", media_type="movie",
                    resolution="1080p", height=1080, width=1920,
                    video_codec="h264", audio_codec="AAC", audio_channels=2,
                    bitrate=5_000_000, size_bytes=1_000_000_000, quality_score=50,
                    file_created_at=datetime.utcnow(),
                ))
                db.add(MetadataCache(
                    cache_key="movie:_sync_test_movie_",
                    title="_Sync_Test_Movie_", media_type="movie",
                    tmdb_id=999998, updated_at=datetime.utcnow() - timedelta(days=30),
                ))
                await db.commit()

            with patch("media_manager.web.services.metadata_sync._populate_media_details", new=AsyncMock()) as mock_populate, \
                 patch("asyncio.sleep", new=AsyncMock()):
                await run_metadata_sync()
                mock_populate.assert_called_once()
        asyncio.run(run())

    def test_tv_item_uses_series_name(self):
        async def run():
            async with AsyncSessionLocal() as db:
                db.add(MediaItem(
                    path=SEEDED_PATHS[1], storage_volume="/media/TvShows",
                    title="1x01 - Pilot", media_type="tv", season=1, episode=1,
                    resolution="1080p", height=1080, width=1920,
                    video_codec="h264", audio_codec="AC3", audio_channels=6,
                    bitrate=5_000_000, size_bytes=1_000_000_000, quality_score=60,
                    file_created_at=datetime.utcnow(),
                ))
                await db.commit()

            with patch("media_manager.web.services.metadata_sync._populate_media_details", new=AsyncMock()) as mock_populate, \
                 patch("asyncio.sleep", new=AsyncMock()):
                await run_metadata_sync()
                mock_populate.assert_called_once()
                chunk_arg = mock_populate.call_args[0][0]
                self.assertEqual(chunk_arg[0]["title"], "_Sync_Test_Show_")
                self.assertEqual(chunk_arg[0]["media_type"], "tv")
        asyncio.run(run())

    def test_exception_is_caught_and_logged(self):
        async def run():
            with patch("media_manager.database.AsyncSessionLocal", side_effect=RuntimeError("db exploded")):
                # Should not raise - run_metadata_sync wraps everything in try/except.
                await run_metadata_sync()
        asyncio.run(run())


class TestWeeklyMetadataSyncScheduler(unittest.TestCase):
    def test_runs_initial_delay_then_sync_then_loops(self):
        """
        The scheduler sleeps 1h, runs a sync, then loops forever sleeping 7
        days between runs. We can't run the real infinite loop, so we mock
        asyncio.sleep to return immediately for the first two calls (the
        initial 1h delay and the first 7-day loop sleep) and then raise
        CancelledError on the third call to break out - this exercises the
        entire function body once without running it forever.
        """
        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 2:
                raise asyncio.CancelledError()

        async def run():
            with patch("media_manager.web.services.metadata_sync.run_metadata_sync", new=AsyncMock()) as mock_run, \
                 patch("asyncio.sleep", new=fake_sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await weekly_metadata_sync_scheduler()
                mock_run.assert_called_once()
                self.assertEqual(sleep_calls, [3600, 604800])
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
