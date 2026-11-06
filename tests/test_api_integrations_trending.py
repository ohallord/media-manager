import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from media_manager.web.app import app
from media_manager.database import get_async_db
from media_manager.web.api import integrations as integrations_module
from tests.test_api_integrations import _fake_config, _override_db_with_config, _make_upcoming_movies_db


def _admin_user():
    return {"username": "admin", "role": "admin"}


class TestApiTrending(unittest.TestCase):
    def setUp(self):
        from media_manager.web.api.auth import get_current_user, get_admin_user
        app.dependency_overrides[get_current_user] = lambda: _admin_user()
        app.dependency_overrides[get_admin_user] = lambda: _admin_user()
        self.client = TestClient(app)

        integrations_module.IN_MEMORY_TRENDING_CACHE.clear()
        integrations_module.IN_MEMORY_RADARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_SONARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_CAST_CACHE.clear()
        integrations_module.IN_MEMORY_TV_EXTERNAL_IDS_CACHE.clear()
        integrations_module.IN_MEMORY_TV_WATCH_PROVIDERS_CACHE.clear()
        integrations_module.IN_MEMORY_MOVIE_WATCH_PROVIDERS_CACHE.clear()
        integrations_module.IN_MEMORY_TV_NETWORKS_CACHE.clear()

    def tearDown(self):
        app.dependency_overrides.clear()
        integrations_module.IN_MEMORY_TRENDING_CACHE.clear()
        integrations_module.IN_MEMORY_RADARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_SONARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_CAST_CACHE.clear()
        integrations_module.IN_MEMORY_TV_EXTERNAL_IDS_CACHE.clear()
        integrations_module.IN_MEMORY_TV_WATCH_PROVIDERS_CACHE.clear()
        integrations_module.IN_MEMORY_MOVIE_WATCH_PROVIDERS_CACHE.clear()
        integrations_module.IN_MEMORY_TV_NETWORKS_CACHE.clear()

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_trending_movies_filters_and_flags_status(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.get_trending = AsyncMock(return_value=[
            {
                "id": 1, "title": "Popular Movie", "release_date": "2026-01-01",
                "overview": "An overview.", "poster_path": "/p1.jpg",
                "genre_ids": [28], "popularity": 90.0, "vote_average": 8.0
            },
            {
                # Missing overview -> excluded
                "id": 2, "title": "No Overview", "release_date": "2026-01-01",
                "overview": "", "poster_path": "/p2.jpg", "genre_ids": []
            },
            {
                "id": 3, "title": "Already Owned Movie", "release_date": "2026-01-01",
                "overview": "ov", "poster_path": "/p3.jpg", "genre_ids": []
            },
        ])
        client_instance.get_movie_genres = AsyncMock(return_value={28: "Action"})
        client_instance.get_movie_details = AsyncMock(return_value={"credits": {"cast": []}})
        client_instance.get_movie_watch_providers = AsyncMock(return_value=["Netflix"])

        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
            tmdb_config=_fake_config("tmdb"),
            local_titles=["Already Owned Movie"],
        )
        res = self.client.get("/api/v1/integrations/trending?media_type=movie&window=week")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["media_type"], "movie")
        self.assertEqual(data["window"], "week")
        titles = [i["title"] for i in data["items"]]
        self.assertEqual(titles, ["Popular Movie", "Already Owned Movie"])
        by_title = {i["title"]: i for i in data["items"]}
        self.assertEqual(by_title["Popular Movie"]["status"], "not_added")
        self.assertEqual(by_title["Popular Movie"]["genres"], ["Action"])
        self.assertEqual(by_title["Already Owned Movie"]["status"], "in_library")
        self.assertEqual(by_title["Popular Movie"]["streaming_providers"], ["Netflix"])

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_trending_tv_flags_in_sonarr_status(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.get_trending = AsyncMock(return_value=[
            {
                "id": 5, "name": "Trending Show", "first_air_date": "2026-01-01",
                "overview": "ov", "poster_path": "/p5.jpg", "genre_ids": []
            },
        ])
        client_instance.get_tv_genres = AsyncMock(return_value={})
        client_instance.get_tv_details = AsyncMock(return_value={"credits": {"cast": []}, "networks": [{"name": "AMC"}]})
        client_instance.get_tv_external_ids = AsyncMock(return_value={"tvdb_id": 500})
        client_instance.get_tv_watch_providers = AsyncMock(return_value=["Hulu"])

        with patch("media_manager.web.api.integrations.SonarrClient") as mock_sonarr_cls:
            mock_sonarr_cls.return_value.get_series = AsyncMock(return_value=[{"tvdbId": 500}])
            app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
                tmdb_config=_fake_config("tmdb"),
                sonarr_config=_fake_config("sonarr"),
            )
            res = self.client.get("/api/v1/integrations/trending?media_type=tv&window=day")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["items"][0]["status"], "in_sonarr")
        self.assertEqual(data["items"][0]["tvdb_id"], 500)
        self.assertEqual(data["items"][0]["streaming_providers"], ["Hulu"])
        self.assertEqual(data["items"][0]["networks"], ["AMC"])

    def test_defaults_invalid_media_type_and_window(self):
        client_dummy_type = type("Dummy", (), {})()

        async def get_trending(*args, **kwargs):
            return []

        async def get_movie_genres():
            return {}

        client_dummy_type.get_trending = get_trending
        client_dummy_type.get_movie_genres = get_movie_genres

        with patch("media_manager.web.api.integrations.TMDbClient", return_value=client_dummy_type):
            app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
                tmdb_config=_fake_config("tmdb")
            )
            res = self.client.get("/api/v1/integrations/trending?media_type=bogus&window=bogus")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["media_type"], "movie")
        self.assertEqual(data["window"], "week")

    def test_fails_when_tmdb_not_configured(self):
        app.dependency_overrides[get_async_db] = _override_db_with_config(None)
        res = self.client.get("/api/v1/integrations/trending")
        self.assertEqual(res.status_code, 400)
        self.assertIn("tmdb", res.json()["detail"].lower())

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_caches_per_media_type_and_window(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.get_trending = AsyncMock(return_value=[])
        client_instance.get_movie_genres = AsyncMock(return_value={})

        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
            tmdb_config=_fake_config("tmdb")
        )
        res1 = self.client.get("/api/v1/integrations/trending?media_type=movie&window=week")
        res2 = self.client.get("/api/v1/integrations/trending?media_type=movie&window=week")

        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res2.status_code, 200)
        client_instance.get_trending.assert_awaited_once()

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_returns_titles_when_watch_providers_hang(self, mock_tmdb_cls):
        """Stalled TMDb enrichment must still return JSON, not hang until a 504 HTML page."""
        client_instance = mock_tmdb_cls.return_value
        client_instance.get_trending = AsyncMock(return_value=[
            {
                "id": 1, "title": "Popular Movie", "release_date": "2026-01-01",
                "overview": "An overview.", "poster_path": "/p1.jpg",
                "genre_ids": [], "popularity": 90.0, "vote_average": 8.0
            },
        ])
        client_instance.get_movie_genres = AsyncMock(return_value={})
        client_instance.get_movie_details = AsyncMock(return_value={"credits": {"cast": []}})
        client_instance.extract_trailer_url = MagicMock(return_value=None)

        async def hang(_tid):
            await asyncio.sleep(30)
            return ["Netflix"]

        client_instance.get_movie_watch_providers = hang

        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
            tmdb_config=_fake_config("tmdb"),
        )
        started = time.monotonic()
        with patch.object(integrations_module, "TRENDING_ENRICH_TIMEOUT_SECONDS", 0.2):
            res = self.client.get("/api/v1/integrations/trending?media_type=movie&window=week")
        elapsed = time.monotonic() - started

        self.assertEqual(res.status_code, 200)
        self.assertLess(elapsed, 5)
        data = res.json()
        self.assertEqual(data["items"][0]["title"], "Popular Movie")
        self.assertEqual(data["items"][0]["streaming_providers"], [])

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_returns_empty_list_when_tmdb_trending_times_out(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.get_trending = AsyncMock(side_effect=asyncio.TimeoutError)
        client_instance.get_movie_genres = AsyncMock(return_value={})

        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
            tmdb_config=_fake_config("tmdb"),
        )
        res = self.client.get("/api/v1/integrations/trending?media_type=movie&window=week")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["items"], [])
        # Timed-out list must not be cached as empty for the full TTL.
        self.assertNotIn(("movie", "week"), integrations_module.IN_MEMORY_TRENDING_CACHE)


if __name__ == "__main__":
    unittest.main()
