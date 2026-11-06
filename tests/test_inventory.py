import unittest
import asyncio
from datetime import datetime
from sqlalchemy import delete
from media_manager.web.api.inventory import get_tv_show_name
from media_manager.database import AsyncSessionLocal
from media_manager.models import MediaItem
from media_manager.web.api.inventory import list_inventory_items

class TestInventoryLogic(unittest.TestCase):

    def test_get_tv_show_name(self):
        # Test standard Season folder path
        path1 = "/media/TvShows/How I Met Your Mother/Season 7/7x17 - No Pressure.mkv"
        self.assertEqual(get_tv_show_name(path1), "How I Met Your Mother")

        # Test path with leading "The"
        path2 = "/media/TvShows/The Big Bang Theory/Season 8/8x08 - The Prom.mkv"
        self.assertEqual(get_tv_show_name(path2), "The Big Bang Theory")

        # Test parent folder fallback
        path3 = "/media/TvShows/Breaking Bad/episode1.mkv"
        self.assertEqual(get_tv_show_name(path3), "Breaking Bad")

    def test_letter_clean_prefix(self):
        shows = ["The Sopranos", "Scrubs", "The Big Bang Theory", "How I Met Your Mother", "1883"]
        
        # Test Letter S
        s_shows = [s for s in shows if (s.upper()[4:] if s.upper().startswith("THE ") else s.upper()).startswith("S")]
        self.assertIn("The Sopranos", s_shows)
        self.assertIn("Scrubs", s_shows)
        self.assertNotIn("How I Met Your Mother", s_shows)

        # Test Letter B
        b_shows = [s for s in shows if (s.upper()[4:] if s.upper().startswith("THE ") else s.upper()).startswith("B")]
        self.assertIn("The Big Bang Theory", b_shows)
        self.assertNotIn("The Sopranos", b_shows)

        # Test Letter H
        h_shows = [s for s in shows if (s.upper()[4:] if s.upper().startswith("THE ") else s.upper()).startswith("H")]
        self.assertIn("How I Met Your Mother", h_shows)

        # Test Number #
        num_shows = [s for s in shows if (s.upper()[4:] if s.upper().startswith("THE ") else s.upper())[0].isdigit()]
        self.assertIn("1883", num_shows)

class TestAsyncInventoryAPI(unittest.TestCase):
    """
    Seeds its own rows rather than assuming a real, already-scanned library
    exists - these must pass in a completely empty/fresh database (e.g. a
    clean CI run), not just against a developer's real local inventory.
    """

    SEEDED_PATHS = [
        "/media/Movies/_CI_Test_Movie_ (2020)/_CI_Test_Movie_ (2020).mkv",
        "/media/TvShows/The Sopranos/Season 1/1x01 - Pilot.mkv",
        "/media/TvShows/How I Met Your Mother/Season 1/1x01 - Pilot.mkv",
    ]

    def setUp(self):
        asyncio.run(self._seed())

    def tearDown(self):
        asyncio.run(self._cleanup())

    async def _seed(self):
        async with AsyncSessionLocal() as db:
            db.add(MediaItem(
                path=self.SEEDED_PATHS[0], storage_volume="/media/Movies",
                title="_CI_Test_Movie_", media_type="movie",
                resolution="1080p", height=1080, width=1920,
                video_codec="h264", audio_codec="AAC", audio_channels=2,
                bitrate=5_000_000, size_bytes=1_000_000_000, quality_score=50,
                file_created_at=datetime.utcnow(),
            ))
            db.add(MediaItem(
                path=self.SEEDED_PATHS[1], storage_volume="/media/TvShows",
                title="The Sopranos", media_type="tv", season=1, episode=1,
                resolution="1080p", height=1080, width=1920,
                video_codec="h264", audio_codec="AC3", audio_channels=6,
                bitrate=5_000_000, size_bytes=1_000_000_000, quality_score=60,
                file_created_at=datetime.utcnow(),
            ))
            db.add(MediaItem(
                path=self.SEEDED_PATHS[2], storage_volume="/media/TvShows",
                title="How I Met Your Mother", media_type="tv", season=1, episode=1,
                resolution="720p", height=720, width=1280,
                video_codec="h264", audio_codec="AAC", audio_channels=2,
                bitrate=3_000_000, size_bytes=500_000_000, quality_score=30,
                file_created_at=datetime.utcnow(),
            ))
            await db.commit()

    async def _cleanup(self):
        async with AsyncSessionLocal() as db:
            await db.execute(delete(MediaItem).where(MediaItem.path.in_(self.SEEDED_PATHS)))
            await db.commit()

    def test_async_inventory_movie_filter(self):
        async def run():
            async with AsyncSessionLocal() as db:
                res = await list_inventory_items(media_type='movie', limit=20, offset=0, db=db, q=None, resolution=None, audio_codec=None, letter=None)
                self.assertGreater(res['total'], 0)
                for item in res['items']:
                    self.assertEqual(item['media_type'], 'movie')

        asyncio.run(run())

    def test_async_inventory_tv_letter_s(self):
        async def run():
            async with AsyncSessionLocal() as db:
                res = await list_inventory_items(media_type='tv', letter='S', limit=5000, offset=0, db=db, q=None, resolution=None, audio_codec=None)
                self.assertGreater(res['total'], 0)
                show_names = set(get_tv_show_name(item['path']) for item in res['items'])
                self.assertIn("The Sopranos", show_names)
                self.assertNotIn("How I Met Your Mother", show_names)

        asyncio.run(run())

if __name__ == '__main__':
    unittest.main()
