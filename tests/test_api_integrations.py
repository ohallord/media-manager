import unittest
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from media_manager.web.app import app
from media_manager.database import get_async_db
from media_manager.web.api import integrations as integrations_module
from media_manager.integrations.tmdb import TMDbClient


def _fake_config(service_name):
    config = type("Cfg", (), {})()
    config.service_name = service_name
    config.api_url = "https://example.invalid"
    config.api_key_encrypted = "fake-key"
    config.enabled = True
    return config


def _override_db_with_config(config):
    class FakeResult:
        def scalar_one_or_none(self):
            return config

    class FakeSession:
        async def execute(self, *args, **kwargs):
            return FakeResult()

        def add(self, *args, **kwargs):
            pass

        async def commit(self):
            pass

        async def rollback(self):
            pass

    async def fake_get_async_db():
        yield FakeSession()

    return fake_get_async_db


class _FakeQueryResult:
    def __init__(self, scalar=None, rows=None, scalars_list=None):
        self._scalar = scalar
        self._rows = rows if rows is not None else []
        self._scalars_list = scalars_list if scalars_list is not None else []

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows

    def scalars(self):
        items = self._scalars_list

        class _Scalars:
            def all(self_inner):
                return items

        return _Scalars()


def _make_upcoming_movies_db(tmdb_config=None, radarr_config=None, sonarr_config=None, local_titles=None, cached_cast_rows=None, playback_stats=None):
    """
    Routing fake DB session for the /upcoming-movies and /upcoming-tv endpoints:
    dispatches each execute() call based on which table/service the query targets,
    so tests don't need to hardcode the endpoint's internal query order.
    """
    local_titles = local_titles or []
    cached_cast_rows = cached_cast_rows or []
    playback_stats = playback_stats or []

    class FakeSession:
        async def execute(self, statement, *args, **kwargs):
            compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
            if "integration_configs" in compiled and "'tmdb'" in compiled:
                return _FakeQueryResult(scalar=tmdb_config)
            if "integration_configs" in compiled and "'radarr'" in compiled:
                return _FakeQueryResult(scalar=radarr_config)
            if "integration_configs" in compiled and "'sonarr'" in compiled:
                return _FakeQueryResult(scalar=sonarr_config)
            if "playback_stats" in compiled:
                return _FakeQueryResult(scalars_list=playback_stats)
            if "media_items" in compiled:
                items = []
                for t in local_titles:
                    it = type("MI", (), {})()
                    it.id = "id-" + t
                    it.title = t
                    it.path = f"/mnt/Movies/{t}.mkv"
                    it.quality_score = 100
                    it.resolution = "1080p"
                    it.size_bytes = 1
                    it.season = None
                    it.episode = None
                    it.video_codec = "h264"
                    it.audio_codec = "aac"
                    items.append(it)
                return _FakeQueryResult(rows=[(t,) for t in local_titles], scalars_list=items)
            if "movie_cast_cache" in compiled:
                return _FakeQueryResult(scalars_list=cached_cast_rows)
            if "metadata_cache" in compiled:
                return _FakeQueryResult(scalars_list=[])
            if "app_log_events" in compiled:
                return _FakeQueryResult(scalars_list=[])
            raise AssertionError(f"Unexpected query in test: {compiled}")

        def add(self, *args, **kwargs):
            pass

        async def commit(self):
            pass

        async def rollback(self):
            pass

    async def fake_get_async_db():
        yield FakeSession()

    return fake_get_async_db


class TestApiIntegrationsAdd(unittest.TestCase):
    def setUp(self):
        from media_manager.web.api.auth import get_current_user, get_admin_user
        admin_user = {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_admin_user] = lambda: admin_user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    @patch("media_manager.web.api.integrations.RadarrClient")
    def test_add_movie_without_directory_uses_radarr_defaults(self, mock_radarr_cls):
        client_instance = mock_radarr_cls.return_value
        client_instance.get_profiles = AsyncMock(return_value=[{"id": 7, "name": "HD-1080p"}])
        client_instance.get_root_folders = AsyncMock(return_value=[{"id": 1, "path": "/media/Movies/"}])
        client_instance.add_movie = AsyncMock(return_value={"success": True, "data": {}})

        app.dependency_overrides[get_async_db] = _override_db_with_config(_fake_config("radarr"))
        res = self.client.post(
            "/api/v1/integrations/add",
            json={
                "type": "movie",
                "media_data": {"title": "Example Movie", "tmdbId": 123, "year": 2024},
            },
        )

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])

        # No root_folder_path / quality_profile_id supplied by the client — the
        # endpoint must resolve them itself from what Radarr already has configured.
        client_instance.add_movie.assert_awaited_once()
        _, kwargs = client_instance.add_movie.call_args
        self.assertEqual(kwargs["root_folder_path"], "/media/Movies/")
        self.assertEqual(kwargs["quality_profile_id"], 7)

    @patch("media_manager.web.api.integrations.SonarrClient")
    def test_add_series_without_directory_uses_sonarr_defaults(self, mock_sonarr_cls):
        client_instance = mock_sonarr_cls.return_value
        client_instance.get_profiles = AsyncMock(return_value=[{"id": 3, "name": "HD-720p"}])
        client_instance.get_root_folders = AsyncMock(return_value=[{"id": 1, "path": "/media/TvShows/"}])
        client_instance.add_series = AsyncMock(return_value={"success": True, "data": {}})

        app.dependency_overrides[get_async_db] = _override_db_with_config(_fake_config("sonarr"))
        res = self.client.post(
            "/api/v1/integrations/add",
            json={
                "type": "tv",
                "media_data": {"title": "Example Show", "tvdbId": 456, "year": 2024},
            },
        )

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])

        client_instance.add_series.assert_awaited_once()
        _, kwargs = client_instance.add_series.call_args
        self.assertEqual(kwargs["root_folder_path"], "/media/TvShows/")
        self.assertEqual(kwargs["quality_profile_id"], 3)

    @patch("media_manager.web.api.integrations.RadarrClient")
    def test_add_movie_respects_explicit_overrides(self, mock_radarr_cls):
        client_instance = mock_radarr_cls.return_value
        client_instance.get_profiles = AsyncMock(return_value=[{"id": 7, "name": "HD-1080p"}])
        client_instance.get_root_folders = AsyncMock(return_value=[
            {"id": 1, "path": "/media/Movies/"}, {"id": 2, "path": "/mnt/OtherMovies/"},
        ])
        client_instance.add_movie = AsyncMock(return_value={"success": True, "data": {}})

        app.dependency_overrides[get_async_db] = _override_db_with_config(_fake_config("radarr"))
        res = self.client.post(
            "/api/v1/integrations/add",
            json={
                "type": "movie",
                "media_data": {"title": "Example Movie", "tmdbId": 123, "year": 2024},
                "quality_profile_id": 99,
                "root_folder_path": "/mnt/OtherMovies/",
            },
        )

        self.assertEqual(res.status_code, 200)
        client_instance.get_profiles.assert_not_awaited()
        # The explicit root folder is still checked against the ones Radarr has.
        client_instance.get_root_folders.assert_awaited_once()
        _, kwargs = client_instance.add_movie.call_args
        self.assertEqual(kwargs["root_folder_path"], "/mnt/OtherMovies/")
        self.assertEqual(kwargs["quality_profile_id"], 99)

    @patch("media_manager.web.api.integrations.RadarrClient")
    def test_add_movie_fails_clearly_when_radarr_has_no_root_folder(self, mock_radarr_cls):
        client_instance = mock_radarr_cls.return_value
        client_instance.get_profiles = AsyncMock(return_value=[{"id": 1, "name": "Any"}])
        client_instance.get_root_folders = AsyncMock(return_value=[])

        app.dependency_overrides[get_async_db] = _override_db_with_config(_fake_config("radarr"))
        res = self.client.post(
            "/api/v1/integrations/add",
            json={
                "type": "movie",
                "media_data": {"title": "Example Movie", "tmdbId": 123, "year": 2024},
            },
        )

        self.assertEqual(res.status_code, 400)
        self.assertIn("root folder", res.json()["detail"].lower())

    def test_add_movie_fails_when_radarr_not_configured(self):
        app.dependency_overrides[get_async_db] = _override_db_with_config(None)
        res = self.client.post(
            "/api/v1/integrations/add",
            json={
                "type": "movie",
                "media_data": {"title": "Example Movie", "tmdbId": 123, "year": 2024},
            },
        )

        self.assertEqual(res.status_code, 400)
        self.assertIn("not configured", res.json()["detail"].lower())


class TestApiUpcomingMovies(unittest.TestCase):
    def setUp(self):
        from media_manager.web.api.auth import get_current_user, get_admin_user
        admin_user = {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_admin_user] = lambda: admin_user
        self.client = TestClient(app)

        # Module-level in-memory caches must not leak state between tests.
        integrations_module.IN_MEMORY_UPCOMING_DISCOVER_CACHE.clear()
        integrations_module.IN_MEMORY_RADARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_CAST_CACHE.clear()

    def tearDown(self):
        app.dependency_overrides.clear()
        integrations_module.IN_MEMORY_UPCOMING_DISCOVER_CACHE.clear()
        integrations_module.IN_MEMORY_RADARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_CAST_CACHE.clear()

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_upcoming_movies_filters_incomplete_entries_and_flags_status(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.discover_upcoming_movies = AsyncMock(return_value=[
            {
                "id": 1, "title": "Complete Movie", "release_date": "2026-08-01",
                "overview": "An overview.", "poster_path": "/poster1.jpg",
                "backdrop_path": "/bd1.jpg", "vote_average": 7.5, "genre_ids": [28],
                "popularity": 42.0
            },
            {
                # Missing overview/poster -> should be excluded (no confirmed metadata).
                "id": 2, "title": "Bare Announcement", "release_date": "2026-08-05",
                "overview": "", "poster_path": None, "popularity": 50.0
            },
            {
                "id": 3, "title": "Already Owned Movie", "release_date": "2026-08-10",
                "overview": "Already on the shelf.", "poster_path": "/poster3.jpg",
                "genre_ids": [], "popularity": 12.0
            },
            {
                # Has full metadata but too obscure -> should be excluded by the popularity floor.
                "id": 4, "title": "Obscure One-Screen Release", "release_date": "2026-08-12",
                "overview": "A tiny festival release.", "poster_path": "/poster4.jpg",
                "genre_ids": [], "popularity": 0.4
            },
            {
                # Shares a substring ("up") with the short local title "Up" below, but is
                # not actually owned -> must not be flagged in_library.
                "id": 5, "title": "The Uprising", "release_date": "2026-08-15",
                "overview": "An unrelated unowned movie.", "poster_path": "/poster5.jpg",
                "genre_ids": [], "popularity": 10.0
            },
            {
                # Already added to Radarr but not yet on disk -> status "in_radarr".
                "id": 6, "title": "Queued In Radarr", "release_date": "2026-08-20",
                "overview": "Already requested.", "poster_path": "/poster6.jpg",
                "genre_ids": [], "popularity": 20.0
            },
        ])
        client_instance.get_movie_genres = AsyncMock(return_value={28: "Action"})
        client_instance.get_movie_details = AsyncMock(return_value={
            "id": 1,
            "credits": {"cast": [
                {"id": 101, "name": "Actor One", "character": "Hero", "profile_path": "/actor1.jpg"},
                {"id": 102, "name": "Actor Two", "character": "Sidekick", "profile_path": None},
            ]},
            "videos": {"results": [
                {"site": "YouTube", "type": "Trailer", "official": True, "key": "abc123"}
            ]}
        })
        client_instance.get_person_details = AsyncMock(return_value={
            "biography": "Some biography."
        })
        # extract_trailer_url is a staticmethod; wiring up the real implementation here
        # since the whole TMDbClient class is mocked above.
        client_instance.extract_trailer_url = TMDbClient.extract_trailer_url

        with patch("media_manager.web.api.integrations.RadarrClient") as mock_radarr_cls:
            radarr_instance = mock_radarr_cls.return_value
            radarr_instance.get_movies = AsyncMock(return_value=[{"tmdbId": 6}])

            app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
                tmdb_config=_fake_config("tmdb"),
                radarr_config=_fake_config("radarr"),
                local_titles=["Already Owned Movie", "Up"],
            )

            res = self.client.get("/api/v1/integrations/upcoming-movies?days=30")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["days"], 30)
        titles = [m["title"] for m in data["movies"]]
        self.assertEqual(titles, [
            "Complete Movie", "Already Owned Movie", "The Uprising", "Queued In Radarr"
        ])

        by_title = {m["title"]: m for m in data["movies"]}
        self.assertEqual(by_title["Complete Movie"]["status"], "not_added")
        self.assertEqual(by_title["Complete Movie"]["genres"], ["Action"])
        self.assertEqual(by_title["Already Owned Movie"]["status"], "in_library")
        # Exact-match only: substring containment against short local titles like "Up"
        # must not falsely flag an unrelated movie as already owned.
        self.assertEqual(by_title["The Uprising"]["status"], "not_added")
        self.assertEqual(by_title["Queued In Radarr"]["status"], "in_radarr")

        # Top-5 cast with headshots (missing profile photo -> null, not omitted).
        cast = by_title["Complete Movie"]["cast"]
        self.assertEqual(len(cast), 2)
        self.assertEqual(cast[0]["name"], "Actor One")
        self.assertTrue(cast[0]["photo_url"].endswith("/actor1.jpg"))
        self.assertIsNone(cast[1]["photo_url"])
        self.assertEqual(cast[0]["bio"], "")
        client_instance.get_person_details.assert_not_awaited()

        self.assertEqual(by_title["Complete Movie"]["trailer_url"], "https://www.youtube.com/watch?v=abc123")

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_upcoming_movies_caches_discover_results_across_requests(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.discover_upcoming_movies = AsyncMock(return_value=[
            {
                "id": 10, "title": "Cached Movie", "release_date": "2026-08-01",
                "overview": "An overview.", "poster_path": "/poster.jpg",
                "genre_ids": [], "popularity": 30.0
            }
        ])
        client_instance.get_movie_genres = AsyncMock(return_value={})
        client_instance.get_movie_details = AsyncMock(return_value={"credits": {"cast": []}})

        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
            tmdb_config=_fake_config("tmdb")
        )

        res1 = self.client.get("/api/v1/integrations/upcoming-movies?days=30")
        res2 = self.client.get("/api/v1/integrations/upcoming-movies?days=30")

        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res2.status_code, 200)
        # Second request within the TTL window must be served from cache, not TMDb again.
        client_instance.discover_upcoming_movies.assert_awaited_once()
        client_instance.get_movie_genres.assert_awaited_once()

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_upcoming_movies_explicit_date_range_overrides_days(self, mock_tmdb_cls):
        client_instance = mock_tmdb_cls.return_value
        client_instance.discover_upcoming_movies = AsyncMock(return_value=[])
        client_instance.get_movie_genres = AsyncMock(return_value={})

        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
            tmdb_config=_fake_config("tmdb")
        )

        res = self.client.get("/api/v1/integrations/upcoming-movies?start_date=2027-03-01&end_date=2027-03-31")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["start_date"], "2027-03-01")
        self.assertEqual(data["end_date"], "2027-03-31")
        client_instance.discover_upcoming_movies.assert_awaited_once_with("2027-03-01", "2027-03-31")

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_upcoming_movies_excludes_streaming_release_after_prior_theatrical(self, mock_tmdb_cls):
        today = date.today()
        prior_theatrical = (today - timedelta(days=200)).strftime("%Y-%m-%d")
        streaming_in_window = (today + timedelta(days=5)).strftime("%Y-%m-%d")
        new_theatrical_in_window = (today + timedelta(days=10)).strftime("%Y-%m-%d")

        client_instance = mock_tmdb_cls.return_value
        client_instance.discover_upcoming_movies = AsyncMock(return_value=[
            {
                # Already had a theatrical run months ago; only "new" thing in this
                # window is landing on a streaming service -> must be excluded.
                "id": 20, "title": "Already In Theaters", "release_date": prior_theatrical,
                "overview": "Now streaming.", "poster_path": "/p20.jpg",
                "genre_ids": [], "popularity": 40.0
            },
            {
                # First-ever US release is theatrical and falls inside the window ->
                # must still be shown.
                "id": 21, "title": "Brand New Release", "release_date": new_theatrical_in_window,
                "overview": "First release.", "poster_path": "/p21.jpg",
                "genre_ids": [], "popularity": 35.0
            },
        ])
        client_instance.get_movie_genres = AsyncMock(return_value={})
        client_instance.get_movie_details = AsyncMock(return_value={"credits": {"cast": []}})

        with patch(
            "media_manager.web.api.integrations._get_release_info_batch",
            new=AsyncMock(return_value={
                20: [
                    {"type": 3, "release_date": f"{prior_theatrical}T00:00:00.000Z", "note": ""},
                    {"type": 4, "release_date": f"{streaming_in_window}T00:00:00.000Z", "note": "Netflix"},
                ],
                21: [
                    {"type": 3, "release_date": f"{new_theatrical_in_window}T00:00:00.000Z", "note": ""},
                ],
            })
        ):
            app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
                tmdb_config=_fake_config("tmdb")
            )
            res = self.client.get("/api/v1/integrations/upcoming-movies?days=30")

        self.assertEqual(res.status_code, 200)
        titles = [m["title"] for m in res.json()["movies"]]
        self.assertEqual(titles, ["Brand New Release"])
        self.assertEqual(res.json()["movies"][0]["release_location"], "Theaters")

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_upcoming_movies_excludes_prior_theatrical_even_when_already_in_radarr(self, mock_tmdb_cls):
        today = date.today()
        prior_theatrical = (today - timedelta(days=200)).strftime("%Y-%m-%d")
        streaming_in_window = (today + timedelta(days=5)).strftime("%Y-%m-%d")

        client_instance = mock_tmdb_cls.return_value
        client_instance.discover_upcoming_movies = AsyncMock(return_value=[
            {
                # Already had a theatrical run months ago (e.g. "Michael") and is
                # now streaming - the exclusion applies unconditionally, even
                # though the user already added it to Radarr. Coming Soon is for
                # discovering new releases, not a status tracker for existing adds.
                "id": 23, "title": "Tracked In Radarr", "release_date": prior_theatrical,
                "overview": "Now streaming, already tracked.", "poster_path": "/p23.jpg",
                "genre_ids": [], "popularity": 30.0
            },
        ])
        client_instance.get_movie_genres = AsyncMock(return_value={})
        client_instance.get_movie_details = AsyncMock(return_value={"credits": {"cast": []}})

        with patch("media_manager.web.api.integrations.RadarrClient") as mock_radarr_cls, \
             patch(
                "media_manager.web.api.integrations._get_release_info_batch",
                new=AsyncMock(return_value={
                    23: [
                        {"type": 3, "release_date": f"{prior_theatrical}T00:00:00.000Z", "note": ""},
                        {"type": 4, "release_date": f"{streaming_in_window}T00:00:00.000Z", "note": "Netflix"},
                    ],
                })
             ):
            radarr_instance = mock_radarr_cls.return_value
            radarr_instance.get_movies = AsyncMock(return_value=[{"tmdbId": 23}])

            app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
                tmdb_config=_fake_config("tmdb"),
                radarr_config=_fake_config("radarr"),
            )
            res = self.client.get("/api/v1/integrations/upcoming-movies?days=30")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        titles = [m["title"] for m in data["movies"]]
        self.assertEqual(titles, [])

    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_upcoming_movies_shows_first_time_streaming_release(self, mock_tmdb_cls):
        today = date.today()
        streaming_in_window = (today + timedelta(days=5)).strftime("%Y-%m-%d")

        client_instance = mock_tmdb_cls.return_value
        client_instance.discover_upcoming_movies = AsyncMock(return_value=[
            {
                # No prior theatrical release at all - a streaming-only debut
                # (e.g. a Netflix original) must still show up.
                "id": 22, "title": "Streaming Original", "release_date": streaming_in_window,
                "overview": "Streaming debut.", "poster_path": "/p22.jpg",
                "genre_ids": [], "popularity": 25.0
            },
        ])
        client_instance.get_movie_genres = AsyncMock(return_value={})
        client_instance.get_movie_details = AsyncMock(return_value={"credits": {"cast": []}})

        with patch(
            "media_manager.web.api.integrations._get_release_info_batch",
            new=AsyncMock(return_value={
                22: [
                    {"type": 4, "release_date": f"{streaming_in_window}T00:00:00.000Z", "note": "Netflix"},
                ],
            })
        ):
            app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
                tmdb_config=_fake_config("tmdb")
            )
            res = self.client.get("/api/v1/integrations/upcoming-movies?days=30")

        self.assertEqual(res.status_code, 200)
        titles = [m["title"] for m in res.json()["movies"]]
        self.assertEqual(titles, ["Streaming Original"])
        self.assertEqual(res.json()["movies"][0]["release_location"], "Streaming (Netflix)")

    def test_upcoming_movies_fails_when_tmdb_not_configured(self):
        app.dependency_overrides[get_async_db] = _override_db_with_config(None)
        res = self.client.get("/api/v1/integrations/upcoming-movies?days=30")
        self.assertEqual(res.status_code, 400)
        self.assertIn("tmdb", res.json()["detail"].lower())

    def test_upcoming_movies_defaults_invalid_days_to_30(self):
        client_dummy = _DummyTmdbClient()
        with patch("media_manager.web.api.integrations.TMDbClient", return_value=client_dummy):
            app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(
                tmdb_config=_fake_config("tmdb")
            )
            res = self.client.get("/api/v1/integrations/upcoming-movies?days=45")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["days"], 30)

    @patch("media_manager.web.api.integrations.RadarrClient")
    def test_add_by_tmdb_looks_up_radarr_and_uses_defaults(self, mock_radarr_cls):
        client_instance = mock_radarr_cls.return_value
        client_instance.lookup_movie_by_tmdb_id = AsyncMock(return_value={
            "title": "Looked Up Movie", "titleSlug": "looked-up-movie",
            "images": [], "tmdbId": 555, "year": 2026
        })
        client_instance.get_profiles = AsyncMock(return_value=[{"id": 4, "name": "HD"}])
        client_instance.get_root_folders = AsyncMock(return_value=[{"id": 1, "path": "/media/Movies/"}])
        client_instance.add_movie = AsyncMock(return_value={"success": True, "data": {}})

        app.dependency_overrides[get_async_db] = _override_db_with_config(_fake_config("radarr"))
        res = self.client.post("/api/v1/integrations/add-by-tmdb", json={"tmdb_id": 555})

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])
        client_instance.lookup_movie_by_tmdb_id.assert_awaited_once_with(555)
        _, kwargs = client_instance.add_movie.call_args
        self.assertEqual(kwargs["movie_data"]["title"], "Looked Up Movie")
        self.assertEqual(kwargs["quality_profile_id"], 4)
        self.assertEqual(kwargs["root_folder_path"], "/media/Movies/")

    @patch("media_manager.web.api.integrations.RadarrClient")
    def test_add_by_tmdb_404_when_radarr_lookup_finds_nothing(self, mock_radarr_cls):
        client_instance = mock_radarr_cls.return_value
        client_instance.lookup_movie_by_tmdb_id = AsyncMock(return_value=None)

        app.dependency_overrides[get_async_db] = _override_db_with_config(_fake_config("radarr"))
        res = self.client.post("/api/v1/integrations/add-by-tmdb", json={"tmdb_id": 999})

        self.assertEqual(res.status_code, 404)


class _DummyTmdbClient:
    """Minimal async-friendly TMDbClient stand-in for the invalid-days default test."""
    async def discover_upcoming_movies(self, *args, **kwargs):
        return []

    async def get_movie_genres(self):
        return {}


if __name__ == "__main__":
    unittest.main()
