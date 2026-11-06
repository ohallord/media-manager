import asyncio
import unittest
from datetime import datetime

from sqlalchemy import delete, select

from media_manager.database import AsyncSessionLocal
from media_manager.models import MediaItem, PlaybackStat
from media_manager.web.services.playback_lookup import (
    isoformat,
    load_stats_by_media_item,
    merge_stats,
    sources_with_plays,
)

TEST_MOVIE_PATH = "/media/Movies/_PlaybackLookup_Test_Movie_ (2020)/_PlaybackLookup_Test_Movie_ (2020).mkv"


def _fake_stat(source, play_count, last_played_at=None):
    stat = type("PS", (), {})()
    stat.source = source
    stat.play_count = play_count
    stat.last_played_at = last_played_at
    return stat


class TestIsoformat(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(isoformat(None))

    def test_datetime_gets_z_suffix(self):
        self.assertEqual(isoformat(datetime(2026, 1, 1)), "2026-01-01T00:00:00Z")


class TestMergeStats(unittest.TestCase):
    def test_empty_list_returns_zero_and_none(self):
        self.assertEqual(merge_stats([]), (0, None))

    def test_takes_max_play_count_across_sources(self):
        stats = [_fake_stat("plex", 2, datetime(2026, 1, 1)), _fake_stat("jellyfin", 5, datetime(2025, 1, 1))]
        play_count, last_played = merge_stats(stats)
        self.assertEqual(play_count, 5)
        self.assertEqual(last_played, datetime(2026, 1, 1))

    def test_ignores_none_last_played_at_when_taking_max(self):
        stats = [_fake_stat("plex", 0, None), _fake_stat("emby", 3, datetime(2026, 2, 1))]
        play_count, last_played = merge_stats(stats)
        self.assertEqual(play_count, 3)
        self.assertEqual(last_played, datetime(2026, 2, 1))

    def test_all_none_last_played_returns_none(self):
        stats = [_fake_stat("plex", 0, None)]
        self.assertEqual(merge_stats(stats), (0, None))


class TestSourcesWithPlays(unittest.TestCase):
    def test_empty_list_returns_empty(self):
        self.assertEqual(sources_with_plays([]), [])

    def test_only_includes_sources_with_positive_play_count(self):
        stats = [_fake_stat("plex", 3), _fake_stat("emby", 0), _fake_stat("jellyfin", 1)]
        self.assertEqual(sources_with_plays(stats), ["jellyfin", "plex"])

    def test_dedupes_and_sorts(self):
        stats = [_fake_stat("plex", 1), _fake_stat("plex", 2)]
        self.assertEqual(sources_with_plays(stats), ["plex"])


class TestLoadStatsByMediaItem(unittest.TestCase):
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
            await db.commit()

    def test_empty_ids_returns_empty_dict_without_querying(self):
        async def run():
            result = await load_stats_by_media_item(None, [])
            self.assertEqual(result, {})
        asyncio.run(run())

    def test_groups_stats_by_media_item_id(self):
        async def run():
            async with AsyncSessionLocal() as db:
                item = MediaItem(
                    path=TEST_MOVIE_PATH, storage_volume="/media/Movies",
                    title="_PlaybackLookup_Test_Movie_", media_type="movie",
                    resolution="1080p", height=1080, width=1920,
                    video_codec="h264", audio_codec="AAC", audio_channels=2,
                    bitrate=5_000_000, size_bytes=1_000_000_000, quality_score=50,
                    file_created_at=datetime.utcnow(),
                )
                db.add(item)
                await db.commit()
                await db.refresh(item)
                db.add(PlaybackStat(media_item_id=item.id, source="plex", play_count=2))
                db.add(PlaybackStat(media_item_id=item.id, source="emby", play_count=1))
                await db.commit()

                result = await load_stats_by_media_item(db, [item.id])
                self.assertEqual(len(result[item.id]), 2)
                sources = {s.source for s in result[item.id]}
                self.assertEqual(sources, {"plex", "emby"})
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
