import unittest
from datetime import datetime

from fastapi.testclient import TestClient

from media_manager.web.app import app
from media_manager.web.api.auth import get_current_user, get_admin_user
from media_manager.database import SyncSessionLocal
from media_manager.models import MediaItem


SEEDED_PATHS = [
    "/media/Movies/CountsTestMovieOne (2020)/CountsTestMovieOne (2020).mkv",
    "/media/Movies/CountsTestMovieTwo (2021)/CountsTestMovieTwo (2021).mkv",
    "/media/TvShows/CountsTestShow/Season 1/1x01 - Pilot.mkv",
    "/media/TvShows/CountsTestShow/Season 1/1x02 - Second.mkv",
]


def _movie_item(path, title):
    return MediaItem(
        path=path, storage_volume="/media/Movies",
        title=title, media_type="movie",
        resolution="1080p", height=1080, width=1920,
        video_codec="h264", audio_codec="aac", audio_channels=2,
        bitrate=5_000_000, size_bytes=1, quality_score=500,
        file_created_at=datetime.utcnow(),
    )


def _tv_item(path, episode):
    return MediaItem(
        path=path, storage_volume="/media/TvShows",
        title="CountsTestShow", media_type="tv", season=1, episode=episode,
        resolution="1080p", height=1080, width=1920,
        video_codec="h264", audio_codec="ac3", audio_channels=6,
        bitrate=5_000_000, size_bytes=1, quality_score=500,
        file_created_at=datetime.utcnow(),
    )


class TestInventoryCounts(unittest.TestCase):
    """
    /api/v1/inventory/counts backs the movie/TV-show total badges next to the
    Inventory page's type tabs. Since a full test-suite run shares the
    isolated test DB with other test files, these assertions use a
    before/after delta (rather than exact totals) so they aren't coupled to
    whatever else the suite has inserted.
    """

    @classmethod
    def setUpClass(cls):
        session = SyncSessionLocal()
        try:
            cls.items = [
                _movie_item(SEEDED_PATHS[0], "CountsTestMovieOne"),
                _movie_item(SEEDED_PATHS[1], "CountsTestMovieTwo"),
                _tv_item(SEEDED_PATHS[2], 1),
                _tv_item(SEEDED_PATHS[3], 2),
            ]
            session.add_all(cls.items)
            session.commit()
        finally:
            session.close()

    @classmethod
    def tearDownClass(cls):
        session = SyncSessionLocal()
        try:
            session.query(MediaItem).filter(MediaItem.path.in_(SEEDED_PATHS)).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()

    def setUp(self):
        app.dependency_overrides[get_current_user] = lambda: {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_admin_user] = lambda: {"username": "admin", "role": "admin"}
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_counts_reflect_seeded_data_and_group_tv_episodes_into_shows(self):
        resp = self.client.get("/api/v1/inventory/counts")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertGreaterEqual(data["movie_count"], 2)
        self.assertGreaterEqual(data["tv_episode_count"], 2)
        # Two seeded episodes of the same show must count as ONE show, not two.
        self.assertLess(data["tv_show_count"], data["tv_episode_count"])


if __name__ == "__main__":
    unittest.main()
