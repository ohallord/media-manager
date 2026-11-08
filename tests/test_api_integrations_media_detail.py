import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from media_manager.web.app import app
from media_manager.database import get_async_db
from media_manager.web.api import integrations as integrations_module
from tests.test_api_integrations import _fake_config, _override_db_with_config, _make_upcoming_movies_db, _FakeQueryResult


def _admin_user():
    return {"username": "admin", "role": "admin"}


def _fake_stat(media_item_id, source, play_count, last_played_at=None):
    stat = type("PS", (), {})()
    stat.media_item_id = media_item_id
    stat.source = source
    stat.play_count = play_count
    stat.last_played_at = last_played_at
    return stat


class TestApiMediaDetail(unittest.TestCase):
    def setUp(self):
        from media_manager.web.api.auth import get_current_user, get_admin_user
        app.dependency_overrides[get_current_user] = lambda: _admin_user()
        app.dependency_overrides[get_admin_user] = lambda: _admin_user()
        self.client = TestClient(app)

        integrations_module.IN_MEMORY_CAST_CACHE.clear()
        integrations_module.IN_MEMORY_RADARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_SONARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_TV_EXTERNAL_IDS_CACHE.clear()

    def tearDown(self):
        app.dependency_overrides.clear()
        integrations_module.IN_MEMORY_CAST_CACHE.clear()
        integrations_module.IN_MEMORY_RADARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_SONARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_TV_EXTERNAL_IDS_CACHE.clear()

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_movie_detail_includes_related_and_status(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.get_movie_details = AsyncMock(return_value={
            "id": 1, "title": "Main Movie", "release_date": "2026-01-01",
            "overview": "ov", "poster_path": "/p1.jpg", "backdrop_path": "/b1.jpg",
            "vote_average": 7.5, "runtime": 120, "genres": [{"id": 28, "name": "Action"}],
            "credits": {"cast": []}, "videos": {"results": []}
        })
        client_instance.get_movie_recommendations = AsyncMock(return_value=[
            {"id": 2, "title": "Rec Movie", "poster_path": "/p2.jpg", "vote_average": 8.0}
        ])
        client_instance.get_similar_movies = AsyncMock(return_value=[
            {"id": 2, "title": "Rec Movie", "poster_path": "/p2.jpg", "vote_average": 8.0},  # dup, should collapse
            {"id": 3, "title": "Sim Movie", "poster_path": "/p3.jpg", "vote_average": 6.5},
            {"id": 1, "title": "Main Movie", "poster_path": "/p1.jpg"},  # self, must be excluded
        ])
        client_instance.get_movie_watch_providers = AsyncMock(return_value=["Netflix"])

        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
            tmdb_config=_fake_config("tmdb"),
            radarr_config=_fake_config("radarr"),
        )
        with patch("media_manager.web.api.integrations.RadarrClient") as mock_radarr_cls:
            mock_radarr_cls.return_value.get_movies = AsyncMock(return_value=[{"tmdbId": 3}])
            res = self.client.get("/api/v1/integrations/media-detail?tmdb_id=1&type=movie")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["title"], "Main Movie")
        self.assertEqual(data["runtime"], 120)
        self.assertEqual(data["genres"], ["Action"])
        self.assertEqual(data["streaming_providers"], ["Netflix"])

        related_ids = [r["tmdb_id"] for r in data["related"]]
        self.assertEqual(related_ids, [2, 3])  # deduped, self excluded

        by_id = {r["tmdb_id"]: r for r in data["related"]}
        self.assertEqual(by_id[3]["status"], "in_radarr")
        self.assertEqual(by_id[2]["status"], "not_added")
        self.assertEqual(data["local_files"], [])

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_tv_detail_uses_episode_run_time_and_sonarr_status(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.get_tv_details = AsyncMock(return_value={
            "id": 10, "name": "Main Show", "first_air_date": "2026-01-01",
            "overview": "ov", "poster_path": "/p10.jpg", "vote_average": 7.0,
            "episode_run_time": [45], "genres": [], "credits": {"cast": []}, "videos": {"results": []}
        })
        client_instance.get_tv_recommendations = AsyncMock(return_value=[])
        client_instance.get_similar_tv = AsyncMock(return_value=[])
        client_instance.get_tv_watch_providers = AsyncMock(return_value=[])
        client_instance.get_tv_external_ids = AsyncMock(return_value={"tvdb_id": 999})

        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
            tmdb_config=_fake_config("tmdb"),
            sonarr_config=_fake_config("sonarr"),
        )
        with patch("media_manager.web.api.integrations.SonarrClient") as mock_sonarr_cls:
            mock_sonarr_cls.return_value.get_series = AsyncMock(return_value=[{"tvdbId": 999}])
            res = self.client.get("/api/v1/integrations/media-detail?tmdb_id=10&type=tv")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["runtime"], 45)
        self.assertEqual(data["tvdb_id"], 999)
        self.assertEqual(data["status"], "in_sonarr")

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_movie_detail_includes_playback_summary(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.get_movie_details = AsyncMock(return_value={
            "id": 1, "title": "Main Movie", "release_date": "2026-01-01",
            "overview": "ov", "poster_path": "/p1.jpg", "backdrop_path": "/b1.jpg",
            "vote_average": 7.5, "runtime": 120, "genres": [],
            "credits": {"cast": []}, "videos": {"results": []}
        })
        client_instance.get_movie_recommendations = AsyncMock(return_value=[])
        client_instance.get_similar_movies = AsyncMock(return_value=[])
        client_instance.get_movie_watch_providers = AsyncMock(return_value=[])

        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
            tmdb_config=_fake_config("tmdb"),
            local_titles=["Main Movie"],
            playback_stats=[_fake_stat("id-Main Movie", "plex", 5, datetime(2026, 3, 1))],
        )
        res = self.client.get("/api/v1/integrations/media-detail?tmdb_id=1&type=movie")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data["local_files"]), 1)
        self.assertEqual(data["local_files"][0]["play_count"], 5)
        self.assertEqual(data["local_files"][0]["play_sources"], ["plex"])
        self.assertEqual(data["playback"]["play_count"], 5)
        self.assertEqual(data["playback"]["play_sources"], ["plex"])
        self.assertTrue(data["playback"]["last_played_at"].startswith("2026-03-01"))

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_tv_detail_includes_series_playback_rollup(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.get_tv_details = AsyncMock(return_value={
            "id": 10, "name": "Main Show", "first_air_date": "2026-01-01",
            "overview": "ov", "poster_path": "/p10.jpg", "vote_average": 7.0,
            "episode_run_time": [45], "genres": [], "credits": {"cast": []}, "videos": {"results": []}
        })
        client_instance.get_tv_recommendations = AsyncMock(return_value=[])
        client_instance.get_similar_tv = AsyncMock(return_value=[])
        client_instance.get_tv_watch_providers = AsyncMock(return_value=[])
        client_instance.get_tv_external_ids = AsyncMock(return_value={"tvdb_id": 999})

        ep1 = type("MI", (), {})()
        ep1.id = "ep-1"
        ep1.title = "1x01 - Pilot"
        ep1.path = "/media/TvShows/Main Show/Season 1/1x01 - Pilot.mkv"
        ep1.quality_score = 100
        ep1.resolution = "1080p"
        ep1.size_bytes = 1
        ep1.season = 1
        ep1.episode = 1
        ep1.video_codec = "h264"
        ep1.audio_codec = "aac"

        ep2 = type("MI", (), {})()
        ep2.id = "ep-2"
        ep2.title = "1x02 - Second"
        ep2.path = "/media/TvShows/Main Show/Season 1/1x02 - Second.mkv"
        ep2.quality_score = 100
        ep2.resolution = "1080p"
        ep2.size_bytes = 1
        ep2.season = 1
        ep2.episode = 2
        ep2.video_codec = "h264"
        ep2.audio_codec = "aac"

        playback_stats = [
            _fake_stat("ep-1", "plex", 3, datetime(2026, 2, 1)),
            _fake_stat("ep-2", "jellyfin", 0, None),
        ]

        class FakeSession:
            async def execute(self, statement, *args, **kwargs):
                compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
                if "integration_configs" in compiled and "'tmdb'" in compiled:
                    return _FakeQueryResult(scalar=_fake_config("tmdb"))
                if "integration_configs" in compiled and "'sonarr'" in compiled:
                    return _FakeQueryResult(scalar=None)
                if "playback_stats" in compiled:
                    return _FakeQueryResult(scalars_list=playback_stats)
                if "media_items" in compiled:
                    return _FakeQueryResult(scalars_list=[ep1, ep2])
                if "movie_cast_cache" in compiled:
                    return _FakeQueryResult(scalars_list=[])
                if "metadata_cache" in compiled:
                    return _FakeQueryResult(scalars_list=[])
                if "app_log_events" in compiled:
                    return _FakeQueryResult(scalars_list=[])
                raise AssertionError(f"Unexpected query in test: {compiled}")

            def add(self, *args, **kwargs):
                pass

            async def commit(self):
                pass

        async def fake_get_async_db():
            yield FakeSession()

        app.dependency_overrides[get_async_db] = fake_get_async_db
        res = self.client.get("/api/v1/integrations/media-detail?tmdb_id=10&type=tv")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["playback"]["total_plays"], 3)
        self.assertEqual(data["playback"]["episode_count"], 2)
        self.assertEqual(data["playback"]["never_played_episode_count"], 1)
        self.assertEqual(data["playback"]["play_sources"], ["plex"])
        self.assertTrue(data["playback"]["last_played_at"].startswith("2026-02-01"))

    def test_fails_when_tmdb_not_configured(self):
        app.dependency_overrides[get_async_db] = _override_db_with_config(None)
        res = self.client.get("/api/v1/integrations/media-detail?tmdb_id=1&type=movie")
        self.assertEqual(res.status_code, 400)

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_404_when_tmdb_has_no_details(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.get_movie_details = AsyncMock(return_value=None)

        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(tmdb_config=_fake_config("tmdb"))
        res = self.client.get("/api/v1/integrations/media-detail?tmdb_id=999&type=movie")
        self.assertEqual(res.status_code, 404)

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_invalid_type_defaults_to_movie(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.get_movie_details = AsyncMock(return_value={
            "id": 1, "title": "Main Movie", "release_date": "2026-01-01",
            "overview": "ov", "poster_path": "/p1.jpg", "vote_average": 7.0,
            "runtime": 100, "genres": [], "credits": {"cast": []}, "videos": {"results": []}
        })
        client_instance.get_movie_recommendations = AsyncMock(return_value=[])
        client_instance.get_similar_movies = AsyncMock(return_value=[])
        client_instance.get_movie_watch_providers = AsyncMock(return_value=[])

        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(tmdb_config=_fake_config("tmdb"))
        res = self.client.get("/api/v1/integrations/media-detail?tmdb_id=1&type=bogus")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["type"], "movie")

    def _stub_movie(self, mock_tmdb_cls, belongs_to_collection=None, collection=None):
        client_instance = mock_tmdb_cls.return_value
        details = {
            "id": 1, "title": "Part Two", "release_date": "2026-01-01",
            "overview": "ov", "poster_path": "/p1.jpg", "vote_average": 7.5, "runtime": 120,
            "genres": [], "credits": {"cast": []}, "videos": {"results": []},
        }
        if belongs_to_collection is not None:
            details["belongs_to_collection"] = belongs_to_collection
        client_instance.get_movie_details = AsyncMock(return_value=details)
        client_instance.get_movie_recommendations = AsyncMock(return_value=[])
        client_instance.get_similar_movies = AsyncMock(return_value=[])
        client_instance.get_movie_watch_providers = AsyncMock(return_value=[])
        client_instance.get_collection = AsyncMock(return_value=collection)
        return client_instance

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_movie_detail_collection_progress(self, mock_tmdb_cls):
        client_instance = self._stub_movie(
            mock_tmdb_cls,
            belongs_to_collection={"id": 77, "name": "Saga Collection"},
            collection={"id": 77, "name": "Saga Collection", "parts": [
                {"id": 4, "title": "Part Four", "release_date": "", "poster_path": None},
                {"id": 1, "title": "Part Two", "release_date": "2026-01-01", "poster_path": "/p1.jpg"},
                {"id": 3, "title": "Part Three", "release_date": "2028-05-01", "poster_path": "/p3.jpg"},
                {"id": 2, "title": "Part One", "release_date": "2020-01-01", "poster_path": "/p2.jpg"},
                {"id": None, "title": "Broken"},
            ]},
        )
        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
            tmdb_config=_fake_config("tmdb"),
            radarr_config=_fake_config("radarr"),
            local_titles=["Part One"],
        )
        with patch("media_manager.web.api.integrations.RadarrClient") as mock_radarr_cls:
            mock_radarr_cls.return_value.get_movies = AsyncMock(return_value=[{"tmdbId": 3}])
            res = self.client.get("/api/v1/integrations/media-detail?tmdb_id=1&type=movie")

        self.assertEqual(res.status_code, 200)
        client_instance.get_collection.assert_awaited_once_with(77)
        coll = res.json()["collection"]
        self.assertEqual(coll["name"], "Saga Collection")
        self.assertEqual(coll["total"], 4)
        self.assertEqual(coll["owned_count"], 1)
        self.assertEqual(coll["added_count"], 2)
        self.assertEqual([p["tmdb_id"] for p in coll["parts"]], [2, 1, 3, 4])  # by date, undated last
        by_id = {p["tmdb_id"]: p for p in coll["parts"]}
        self.assertEqual(by_id[2]["status"], "in_library")
        self.assertEqual(by_id[3]["status"], "in_radarr")
        self.assertEqual(by_id[1]["status"], "not_added")
        self.assertTrue(by_id[1]["is_current"])
        self.assertFalse(by_id[2]["is_current"])
        self.assertEqual(by_id[2]["year"], "2020")
        self.assertIsNone(by_id[4]["year"])
        self.assertIsNone(by_id[4]["poster_url"])
        self.assertEqual(by_id[3]["poster_url"], "https://image.tmdb.org/t/p/w342/p3.jpg")

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_movie_detail_without_collection_skips_lookup(self, mock_tmdb_cls):
        client_instance = self._stub_movie(mock_tmdb_cls)
        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(tmdb_config=_fake_config("tmdb"))
        res = self.client.get("/api/v1/integrations/media-detail?tmdb_id=1&type=movie")

        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json()["collection"])
        client_instance.get_collection.assert_not_called()

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_movie_detail_collection_lookup_failure_is_non_fatal(self, mock_tmdb_cls):
        self._stub_movie(mock_tmdb_cls, belongs_to_collection={"id": 77}, collection=None)
        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(tmdb_config=_fake_config("tmdb"))
        res = self.client.get("/api/v1/integrations/media-detail?tmdb_id=1&type=movie")

        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json()["collection"])

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_movie_detail_collection_with_no_usable_parts_is_null(self, mock_tmdb_cls):
        self._stub_movie(
            mock_tmdb_cls, belongs_to_collection={"id": 77},
            collection={"id": 77, "name": "Empty", "parts": [{"id": 5}]},
        )
        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(tmdb_config=_fake_config("tmdb"))
        res = self.client.get("/api/v1/integrations/media-detail?tmdb_id=1&type=movie")

        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json()["collection"])

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_tv_detail_has_no_collection(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.get_tv_details = AsyncMock(return_value={
            "id": 10, "name": "Main Show", "episode_run_time": [], "genres": [],
            "credits": {"cast": []}, "videos": {"results": []},
            "belongs_to_collection": {"id": 77},
        })
        client_instance.get_tv_recommendations = AsyncMock(return_value=[])
        client_instance.get_similar_tv = AsyncMock(return_value=[])
        client_instance.get_tv_watch_providers = AsyncMock(return_value=[])
        client_instance.get_tv_external_ids = AsyncMock(return_value=None)
        client_instance.get_collection = AsyncMock(return_value=None)
        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(tmdb_config=_fake_config("tmdb"))
        res = self.client.get("/api/v1/integrations/media-detail?tmdb_id=10&type=tv")

        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json()["collection"])
        client_instance.get_collection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
