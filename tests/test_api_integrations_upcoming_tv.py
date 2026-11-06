import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from media_manager.web.app import app
from media_manager.database import get_async_db
from media_manager.web.api import integrations as integrations_module
from tests.test_api_integrations import _fake_config, _override_db_with_config, _make_upcoming_movies_db


def _admin_user():
    return {"username": "admin", "role": "admin"}


class TestApiUpcomingTv(unittest.TestCase):
    def setUp(self):
        from media_manager.web.api.auth import get_current_user, get_admin_user
        app.dependency_overrides[get_current_user] = lambda: _admin_user()
        app.dependency_overrides[get_admin_user] = lambda: _admin_user()
        self.client = TestClient(app)

        integrations_module.IN_MEMORY_UPCOMING_TV_DISCOVER_CACHE.clear()
        integrations_module.IN_MEMORY_SONARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_CAST_CACHE.clear()
        integrations_module.IN_MEMORY_TV_EXTERNAL_IDS_CACHE.clear()
        integrations_module.IN_MEMORY_TV_WATCH_PROVIDERS_CACHE.clear()
        integrations_module.IN_MEMORY_TV_NETWORKS_CACHE.clear()

    def tearDown(self):
        app.dependency_overrides.clear()
        integrations_module.IN_MEMORY_UPCOMING_TV_DISCOVER_CACHE.clear()
        integrations_module.IN_MEMORY_SONARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_CAST_CACHE.clear()
        integrations_module.IN_MEMORY_TV_EXTERNAL_IDS_CACHE.clear()
        integrations_module.IN_MEMORY_TV_WATCH_PROVIDERS_CACHE.clear()
        integrations_module.IN_MEMORY_TV_NETWORKS_CACHE.clear()

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_upcoming_tv_filters_incomplete_entries_and_flags_status(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.discover_upcoming_tv = AsyncMock(return_value=[
            {
                "id": 1, "name": "Complete Show", "first_air_date": "2026-08-01",
                "overview": "An overview.", "poster_path": "/poster1.jpg",
                "backdrop_path": "/bd1.jpg", "vote_average": 7.5, "genre_ids": [10759],
                "popularity": 42.0
            },
            {
                # Missing overview/poster -> should be excluded (no confirmed metadata).
                "id": 2, "name": "Bare Announcement", "first_air_date": "2026-08-05",
                "overview": "", "poster_path": None, "popularity": 50.0
            },
            {
                "id": 3, "name": "Already Owned Show", "first_air_date": "2026-08-10",
                "overview": "Already on the shelf.", "poster_path": "/poster3.jpg",
                "genre_ids": [], "popularity": 12.0
            },
            {
                # Too obscure -> excluded by the popularity floor.
                "id": 4, "name": "Obscure Series", "first_air_date": "2026-08-12",
                "overview": "A tiny festival release.", "poster_path": "/poster4.jpg",
                "genre_ids": [], "popularity": 0.4
            },
            {
                # Already added to Sonarr but not yet on disk -> status "in_sonarr".
                "id": 5, "name": "Queued In Sonarr", "first_air_date": "2026-08-20",
                "overview": "Already requested.", "poster_path": "/poster5.jpg",
                "genre_ids": [], "popularity": 20.0
            },
        ])
        client_instance.get_tv_genres = AsyncMock(return_value={10759: "Action & Adventure"})
        client_instance.get_tv_details = AsyncMock(return_value={
            "credits": {"cast": [
                {"id": 101, "name": "Actor One", "character": "Hero", "profile_path": "/actor1.jpg"},
            ]},
            "videos": {"results": []},
            "networks": [{"name": "Apple TV+"}]
        })
        client_instance.get_person_details = AsyncMock(return_value={"biography": "Bio."})
        client_instance.get_tv_external_ids = AsyncMock(side_effect=lambda tid: {"tvdb_id": tid * 100})
        client_instance.get_tv_watch_providers = AsyncMock(return_value=["Netflix"])

        with patch("media_manager.web.api.integrations.SonarrClient") as mock_sonarr_cls:
            sonarr_instance = mock_sonarr_cls.return_value
            sonarr_instance.get_series = AsyncMock(return_value=[{"tvdbId": 500}])  # id 5 * 100

            app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
                tmdb_config=_fake_config("tmdb"),
                sonarr_config=_fake_config("sonarr"),
                local_titles=["Already Owned Show"],
            )

            res = self.client.get("/api/v1/integrations/upcoming-tv?days=30")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["days"], 30)
        titles = [s["title"] for s in data["shows"]]
        self.assertEqual(titles, [
            "Complete Show", "Already Owned Show", "Queued In Sonarr"
        ])

        by_title = {s["title"]: s for s in data["shows"]}
        self.assertEqual(by_title["Complete Show"]["status"], "not_added")
        self.assertEqual(by_title["Complete Show"]["genres"], ["Action & Adventure"])
        self.assertEqual(by_title["Already Owned Show"]["status"], "in_library")
        self.assertEqual(by_title["Queued In Sonarr"]["status"], "in_sonarr")
        self.assertEqual(by_title["Complete Show"]["streaming_providers"], ["Netflix"])
        self.assertEqual(by_title["Complete Show"]["networks"], ["Apple TV+"])

        cast = by_title["Complete Show"]["cast"]
        self.assertEqual(len(cast), 1)
        self.assertEqual(cast[0]["name"], "Actor One")

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_upcoming_tv_caches_discover_results_across_requests(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.discover_upcoming_tv = AsyncMock(return_value=[
            {
                "id": 10, "name": "Cached Show", "first_air_date": "2026-08-01",
                "overview": "An overview.", "poster_path": "/poster.jpg",
                "genre_ids": [], "popularity": 30.0
            }
        ])
        client_instance.get_tv_genres = AsyncMock(return_value={})
        client_instance.get_tv_details = AsyncMock(return_value={"credits": {"cast": []}})
        client_instance.get_tv_external_ids = AsyncMock(return_value={"tvdb_id": None})
        client_instance.get_tv_watch_providers = AsyncMock(return_value=[])

        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
            tmdb_config=_fake_config("tmdb")
        )

        res1 = self.client.get("/api/v1/integrations/upcoming-tv?days=30")
        res2 = self.client.get("/api/v1/integrations/upcoming-tv?days=30")

        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res2.status_code, 200)
        client_instance.discover_upcoming_tv.assert_awaited_once()
        client_instance.get_tv_genres.assert_awaited_once()

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_upcoming_tv_explicit_date_range_overrides_days(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.discover_upcoming_tv = AsyncMock(return_value=[])
        client_instance.get_tv_genres = AsyncMock(return_value={})

        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
            tmdb_config=_fake_config("tmdb")
        )

        res = self.client.get("/api/v1/integrations/upcoming-tv?start_date=2027-03-01&end_date=2027-03-31")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["start_date"], "2027-03-01")
        self.assertEqual(data["end_date"], "2027-03-31")
        client_instance.discover_upcoming_tv.assert_awaited_once_with("2027-03-01", "2027-03-31")

    def test_upcoming_tv_fails_when_tmdb_not_configured(self):
        app.dependency_overrides[get_async_db] = _override_db_with_config(None)
        res = self.client.get("/api/v1/integrations/upcoming-tv?days=30")
        self.assertEqual(res.status_code, 400)
        self.assertIn("tmdb", res.json()["detail"].lower())

    def test_upcoming_tv_defaults_invalid_days_to_30(self):
        class _DummyTmdbClient:
            async def discover_upcoming_tv(self, *args, **kwargs):
                return []

            async def get_tv_genres(self):
                return {}

        with patch("media_manager.web.api.integrations.TMDbClient", return_value=_DummyTmdbClient()):
            app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
                tmdb_config=_fake_config("tmdb")
            )
            res = self.client.get("/api/v1/integrations/upcoming-tv?days=45")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["days"], 30)

    @patch("media_manager.web.api.integrations.SonarrClient")
    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_add_tv_by_tmdb_looks_up_external_ids_and_sonarr(self, mock_tmdb_cls, mock_sonarr_cls):
        tmdb_instance = mock_tmdb_cls.return_value
        tmdb_instance.get_tv_external_ids = AsyncMock(return_value={"tvdb_id": 12345})

        sonarr_instance = mock_sonarr_cls.return_value
        sonarr_instance.lookup_series_by_tvdb_id = AsyncMock(return_value={
            "title": "Looked Up Show", "titleSlug": "looked-up-show", "images": [], "tvdbId": 12345, "year": 2026
        })
        sonarr_instance.get_profiles = AsyncMock(return_value=[{"id": 4, "name": "HD"}])
        sonarr_instance.get_root_folders = AsyncMock(return_value=[{"id": 1, "path": "/media/TvShows/"}])
        sonarr_instance.add_series = AsyncMock(return_value={"success": True, "data": {}})

        app.dependency_overrides[get_async_db] = _override_db_with_config(_fake_config("sonarr"))
        res = self.client.post("/api/v1/integrations/add-tv-by-tmdb", json={"tmdb_id": 999})

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])
        tmdb_instance.get_tv_external_ids.assert_awaited_once_with(999)
        sonarr_instance.lookup_series_by_tvdb_id.assert_awaited_once_with(12345)
        _, kwargs = sonarr_instance.add_series.call_args
        self.assertEqual(kwargs["series_data"]["title"], "Looked Up Show")
        self.assertEqual(kwargs["quality_profile_id"], 4)
        self.assertEqual(kwargs["root_folder_path"], "/media/TvShows/")

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_add_tv_by_tmdb_404_when_no_tvdb_id(self, mock_tmdb_cls):
        tmdb_instance = mock_tmdb_cls.return_value
        tmdb_instance.get_tv_external_ids = AsyncMock(return_value={"tvdb_id": None})

        app.dependency_overrides[get_async_db] = _override_db_with_config(_fake_config("sonarr"))
        res = self.client.post("/api/v1/integrations/add-tv-by-tmdb", json={"tmdb_id": 999})
        self.assertEqual(res.status_code, 404)

    @patch("media_manager.web.api.integrations.SonarrClient")
    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_add_tv_by_tmdb_404_when_sonarr_lookup_finds_nothing(self, mock_tmdb_cls, mock_sonarr_cls):
        tmdb_instance = mock_tmdb_cls.return_value
        tmdb_instance.get_tv_external_ids = AsyncMock(return_value={"tvdb_id": 12345})
        sonarr_instance = mock_sonarr_cls.return_value
        sonarr_instance.lookup_series_by_tvdb_id = AsyncMock(return_value=None)

        app.dependency_overrides[get_async_db] = _override_db_with_config(_fake_config("sonarr"))
        res = self.client.post("/api/v1/integrations/add-tv-by-tmdb", json={"tmdb_id": 999})
        self.assertEqual(res.status_code, 404)

    def test_add_tv_by_tmdb_fails_when_sonarr_not_configured(self):
        app.dependency_overrides[get_async_db] = _override_db_with_config(None)
        res = self.client.post("/api/v1/integrations/add-tv-by-tmdb", json={"tmdb_id": 999})
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
