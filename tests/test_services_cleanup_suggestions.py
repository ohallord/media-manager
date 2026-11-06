import asyncio
import unittest
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from media_manager.database import AsyncSessionLocal
from media_manager.models import CleanupSettings, MediaItem, PlaybackStat
from media_manager.web.services.cleanup_suggestions import (
    compute_suggestions,
    get_or_create_settings,
)

NOW = datetime.utcnow()
OLD_NEVER_PLAYED = NOW - timedelta(days=200)   # past default 180-day grace
RECENT_ADD = NOW - timedelta(days=10)          # within default 180-day grace
OLD_LAST_PLAYED = NOW - timedelta(days=400)    # past default 365-day stale threshold
RECENT_LAST_PLAYED = NOW - timedelta(days=5)

TEST_PATHS = [
    "/media/Movies/_Cleanup_Never_Played_Old_/_Cleanup_Never_Played_Old_ (2020).mkv",
    "/media/Movies/_Cleanup_Never_Played_Recent_/_Cleanup_Never_Played_Recent_ (2020).mkv",
    "/media/Movies/_Cleanup_Stale_Old_/_Cleanup_Stale_Old_ (2020).mkv",
    "/media/Movies/_Cleanup_Recently_Played_/_Cleanup_Recently_Played_ (2020).mkv",
    "/media/Movies/_Cleanup_Unsynced_/_Cleanup_Unsynced_ (2020).mkv",
    "/media/TvShows/_Cleanup_Test_Show_Full_/Season 1/1x01 - A.mkv",
    "/media/TvShows/_Cleanup_Test_Show_Full_/Season 1/1x02 - B.mkv",
    "/media/TvShows/_Cleanup_Test_Show_Partial_/Season 1/1x01 - A.mkv",
    "/media/TvShows/_Cleanup_Test_Show_Partial_/Season 1/1x02 - B.mkv",
    "/media/TvShows/_Cleanup_Test_Show_Active_/Season 1/1x01 - A.mkv",
]
TEST_MEDIA_TYPES = ["movie"] * 5 + ["tv"] * 5


class TestCleanupSuggestionsBase(unittest.TestCase):
    def setUp(self):
        asyncio.run(self._cleanup())

    def tearDown(self):
        asyncio.run(self._cleanup())

    async def _cleanup(self):
        async with AsyncSessionLocal() as db:
            item_ids_res = await db.execute(select(MediaItem.id).where(MediaItem.path.in_(TEST_PATHS)))
            item_ids = [row[0] for row in item_ids_res.all()]
            if item_ids:
                await db.execute(delete(PlaybackStat).where(PlaybackStat.media_item_id.in_(item_ids)))
            await db.execute(delete(MediaItem).where(MediaItem.path.in_(TEST_PATHS)))
            await db.execute(delete(CleanupSettings).where(CleanupSettings.media_type.in_(["movie", "tv"])))
            await db.commit()

    async def _seed_item(self, path, media_type, title, size_bytes=1_000_000_000, file_created_at=None, season=None, episode=None):
        async with AsyncSessionLocal() as db:
            item = MediaItem(
                path=path, storage_volume="/mnt", title=title, media_type=media_type,
                season=season, episode=episode,
                resolution="1080p", height=1080, width=1920,
                video_codec="h264", audio_codec="AAC", audio_channels=2,
                bitrate=5_000_000, size_bytes=size_bytes, quality_score=50,
                file_created_at=file_created_at or NOW,
            )
            db.add(item)
            await db.commit()
            await db.refresh(item)
            return item.id

    async def _seed_stat(self, media_item_id, play_count, last_played_at):
        async with AsyncSessionLocal() as db:
            db.add(PlaybackStat(
                media_item_id=media_item_id, source="plex",
                play_count=play_count, last_played_at=last_played_at,
            ))
            await db.commit()


class TestGetOrCreateSettings(TestCleanupSuggestionsBase):
    def test_creates_defaults_on_first_call(self):
        async def run():
            async with AsyncSessionLocal() as db:
                settings = await get_or_create_settings(db, "movie")
                self.assertEqual(settings.never_played_grace_days, 180)
                self.assertEqual(settings.stale_after_days, 365)
        asyncio.run(run())

    def test_returns_existing_row_on_second_call(self):
        async def run():
            async with AsyncSessionLocal() as db:
                first = await get_or_create_settings(db, "tv")
                first.never_played_grace_days = 30
                await db.commit()

            async with AsyncSessionLocal() as db:
                second = await get_or_create_settings(db, "tv")
                self.assertEqual(second.never_played_grace_days, 30)
        asyncio.run(run())


class TestMovieSuggestions(TestCleanupSuggestionsBase):
    def test_never_played_past_grace_is_suggested(self):
        async def run():
            item_id = await self._seed_item(
                TEST_PATHS[0], "movie", "_Cleanup_Never_Played_Old_", file_created_at=OLD_NEVER_PLAYED
            )
            await self._seed_stat(item_id, 0, None)

            async with AsyncSessionLocal() as db:
                result = await compute_suggestions(db)

            ids = [s["media_item_id"] for s in result["movie"]["suggestions"]]
            self.assertIn(item_id, ids)
            entry = next(s for s in result["movie"]["suggestions"] if s["media_item_id"] == item_id)
            self.assertEqual(entry["reason"], "never_played")
        asyncio.run(run())

    def test_never_played_within_grace_not_suggested(self):
        async def run():
            item_id = await self._seed_item(
                TEST_PATHS[1], "movie", "_Cleanup_Never_Played_Recent_", file_created_at=RECENT_ADD
            )
            await self._seed_stat(item_id, 0, None)

            async with AsyncSessionLocal() as db:
                result = await compute_suggestions(db)

            ids = [s["media_item_id"] for s in result["movie"]["suggestions"]]
            self.assertNotIn(item_id, ids)
        asyncio.run(run())

    def test_stale_after_threshold_is_suggested(self):
        async def run():
            item_id = await self._seed_item(TEST_PATHS[2], "movie", "_Cleanup_Stale_Old_", file_created_at=OLD_NEVER_PLAYED)
            await self._seed_stat(item_id, 3, OLD_LAST_PLAYED)

            async with AsyncSessionLocal() as db:
                result = await compute_suggestions(db)

            entry = next((s for s in result["movie"]["suggestions"] if s["media_item_id"] == item_id), None)
            self.assertIsNotNone(entry)
            self.assertEqual(entry["reason"], "stale")
        asyncio.run(run())

    def test_recently_played_not_suggested(self):
        async def run():
            item_id = await self._seed_item(TEST_PATHS[3], "movie", "_Cleanup_Recently_Played_", file_created_at=OLD_NEVER_PLAYED)
            await self._seed_stat(item_id, 3, RECENT_LAST_PLAYED)

            async with AsyncSessionLocal() as db:
                result = await compute_suggestions(db)

            ids = [s["media_item_id"] for s in result["movie"]["suggestions"]]
            self.assertNotIn(item_id, ids)
        asyncio.run(run())

    def test_unsynced_item_never_suggested(self):
        async def run():
            item_id = await self._seed_item(TEST_PATHS[4], "movie", "_Cleanup_Unsynced_", file_created_at=OLD_NEVER_PLAYED)
            # No PlaybackStat row at all - never matched by a sync.

            async with AsyncSessionLocal() as db:
                result = await compute_suggestions(db)

            ids = [s["media_item_id"] for s in result["movie"]["suggestions"]]
            self.assertNotIn(item_id, ids)
        asyncio.run(run())

    def test_sorted_by_size_descending(self):
        async def run():
            small_id = await self._seed_item(
                TEST_PATHS[0], "movie", "_Cleanup_Never_Played_Old_",
                size_bytes=1_000_000, file_created_at=OLD_NEVER_PLAYED,
            )
            large_id = await self._seed_item(
                TEST_PATHS[2], "movie", "_Cleanup_Stale_Old_",
                size_bytes=9_000_000, file_created_at=OLD_NEVER_PLAYED,
            )
            await self._seed_stat(small_id, 0, None)
            await self._seed_stat(large_id, 0, None)

            async with AsyncSessionLocal() as db:
                result = await compute_suggestions(db)

            sizes = [s["size_bytes"] for s in result["movie"]["suggestions"]]
            self.assertEqual(sizes, sorted(sizes, reverse=True))
        asyncio.run(run())

    def test_total_reclaimable_bytes_sums_suggestions(self):
        async def run():
            item_id = await self._seed_item(
                TEST_PATHS[0], "movie", "_Cleanup_Never_Played_Old_",
                size_bytes=5_000_000, file_created_at=OLD_NEVER_PLAYED,
            )
            await self._seed_stat(item_id, 0, None)

            async with AsyncSessionLocal() as db:
                result = await compute_suggestions(db)

            self.assertEqual(result["movie"]["total_reclaimable_bytes"], 5_000_000)
        asyncio.run(run())


class TestTvSuggestions(TestCleanupSuggestionsBase):
    def test_fully_synced_never_played_show_is_suggested_as_whole_series(self):
        async def run():
            ep1 = await self._seed_item(
                TEST_PATHS[5], "tv", "1x01 - A", size_bytes=2_000_000, season=1, episode=1,
                file_created_at=OLD_NEVER_PLAYED,
            )
            ep2 = await self._seed_item(
                TEST_PATHS[6], "tv", "1x02 - B", size_bytes=3_000_000, season=1, episode=2,
                file_created_at=OLD_NEVER_PLAYED,
            )
            await self._seed_stat(ep1, 0, None)
            await self._seed_stat(ep2, 0, None)

            async with AsyncSessionLocal() as db:
                result = await compute_suggestions(db)

            entry = next(
                (s for s in result["tv"]["suggestions"] if s["show_name"] == "_Cleanup_Test_Show_Full_"), None
            )
            self.assertIsNotNone(entry)
            self.assertEqual(entry["reason"], "never_played")
            self.assertEqual(entry["episode_count"], 2)
            self.assertEqual(set(entry["episode_media_item_ids"]), {ep1, ep2})
            self.assertEqual(entry["size_bytes"], 5_000_000)
        asyncio.run(run())

    def test_partially_synced_show_is_excluded(self):
        async def run():
            ep1 = await self._seed_item(
                TEST_PATHS[7], "tv", "1x01 - A", season=1, episode=1, file_created_at=OLD_NEVER_PLAYED,
            )
            await self._seed_item(
                TEST_PATHS[8], "tv", "1x02 - B", season=1, episode=2, file_created_at=OLD_NEVER_PLAYED,
            )
            await self._seed_stat(ep1, 0, None)
            # Second episode deliberately has no PlaybackStat row at all.

            async with AsyncSessionLocal() as db:
                result = await compute_suggestions(db)

            names = [s["show_name"] for s in result["tv"]["suggestions"]]
            self.assertNotIn("_Cleanup_Test_Show_Partial_", names)
        asyncio.run(run())

    def test_actively_watched_show_not_suggested(self):
        async def run():
            ep1 = await self._seed_item(
                TEST_PATHS[9], "tv", "1x01 - A", season=1, episode=1, file_created_at=OLD_NEVER_PLAYED,
            )
            await self._seed_stat(ep1, 2, RECENT_LAST_PLAYED)

            async with AsyncSessionLocal() as db:
                result = await compute_suggestions(db)

            names = [s["show_name"] for s in result["tv"]["suggestions"]]
            self.assertNotIn("_Cleanup_Test_Show_Active_", names)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
