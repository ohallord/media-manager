import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from media_manager.web.app import app
from media_manager.database import get_async_db
from media_manager.web.api import integrations as integrations_module
from media_manager.web.api.integrations import (
    _get_radarr_tmdb_ids,
    _get_sonarr_tvdb_ids,
    _get_cast_and_trailer,
    _get_release_info_batch,
    _populate_media_details,
)
from media_manager.models import MovieCastCache, MetadataCache
from tests.test_api_integrations import _fake_config, _override_db_with_config, _make_upcoming_movies_db, _FakeQueryResult


def _admin_user():
    return {"username": "admin", "role": "admin"}


class TestSyncthingDownloads(unittest.TestCase):
    def setUp(self):
        from media_manager.web.api.auth import get_current_user, get_admin_user
        app.dependency_overrides[get_current_user] = lambda: _admin_user()
        app.dependency_overrides[get_admin_user] = lambda: _admin_user()
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_not_configured(self):
        app.dependency_overrides[get_async_db] = _override_db_with_config(None)
        res = self.client.get("/api/v1/integrations/syncthing/downloads")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["folders"], [])

    @patch("media_manager.web.api.integrations.SyncthingClient")
    def test_configured_returns_folder_and_connection_totals(self, mock_cls):
        instance = mock_cls.return_value
        instance.get_folders = AsyncMock(return_value=[{"id": "f1", "label": "Movies", "path": "/mnt/Movies"}])
        instance.get_db_status = AsyncMock(return_value={"state": "idle", "globalBytes": 1000, "needBytes": 250})
        instance.get_db_need = AsyncMock(return_value={
            "progress": [{"name": "a.mkv", "size": 100}],
            "queued": [{"name": "b.mkv", "size": 200}],
        })
        instance.get_connections = AsyncMock(return_value={
            "connections": {"dev1": {"inBytesTotal": 500, "outBytesTotal": 300}}
        })

        app.dependency_overrides[get_async_db] = _override_db_with_config(_fake_config("syncthing"))
        res = self.client.get("/api/v1/integrations/syncthing/downloads")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["enabled"])
        self.assertEqual(len(data["folders"]), 1)
        folder = data["folders"][0]
        self.assertEqual(folder["completion"], 75.0)
        self.assertEqual(folder["need_bytes"], 250)
        self.assertEqual(data["total_need_bytes"], 250)
        self.assertEqual(data["total_in_bytes"], 500)
        self.assertEqual(data["total_out_bytes"], 300)
        names = {f["name"] for f in folder["needed_files"]}
        self.assertEqual(names, {"a.mkv", "b.mkv"})


class TestAddByTmdbAdditionalBranches(unittest.TestCase):
    def setUp(self):
        from media_manager.web.api.auth import get_current_user, get_admin_user
        app.dependency_overrides[get_current_user] = lambda: _admin_user()
        app.dependency_overrides[get_admin_user] = lambda: _admin_user()
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        integrations_module.IN_MEMORY_RADARR_LIBRARY_CACHE.clear()

    @patch("media_manager.web.api.integrations.RadarrClient")
    def test_already_in_radarr_still_logs_and_returns_success(self, mock_radarr_cls):
        instance = mock_radarr_cls.return_value
        instance.lookup_movie_by_tmdb_id = AsyncMock(return_value={"title": "Existing Movie", "tmdbId": 42})
        instance.get_profiles = AsyncMock(return_value=[{"id": 1}])
        instance.get_root_folders = AsyncMock(return_value=[{"path": "/media/Movies/"}])
        instance.add_movie = AsyncMock(return_value={"success": False, "error": "Movie has already been added"})
        instance.get_movies = AsyncMock(return_value=[{"tmdbId": 42, "id": 7, "title": "Existing Movie"}])
        instance.trigger_command = AsyncMock(return_value={"success": True})

        app.dependency_overrides[get_async_db] = _override_db_with_config(_fake_config("radarr"))
        res = self.client.post("/api/v1/integrations/add-by-tmdb", json={"tmdb_id": 42})

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])
        self.assertIn("already in Radarr", res.json()["message"])

    @patch("media_manager.web.api.integrations.RadarrClient")
    def test_metadata_cache_update_failure_does_not_break_request(self, mock_radarr_cls):
        instance = mock_radarr_cls.return_value
        instance.lookup_movie_by_tmdb_id = AsyncMock(return_value={"title": "Cache Fail Movie", "tmdbId": 99})
        instance.get_profiles = AsyncMock(return_value=[{"id": 1}])
        instance.get_root_folders = AsyncMock(return_value=[{"path": "/media/Movies/"}])
        instance.add_movie = AsyncMock(return_value={"success": True, "data": {}})

        class PartiallyBrokenSession:
            """First execute() (radarr config lookup) succeeds; the later
            MetadataCache lookup inside the try/except raises, which the
            endpoint must swallow (logs a warning) rather than 500."""
            def __init__(self):
                self.calls = 0

            async def execute(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return _FakeQueryResult(scalar=_fake_config("radarr"))
                raise RuntimeError("db exploded")

            def add(self, *args, **kwargs):
                pass

            async def commit(self):
                pass

            async def rollback(self):
                pass

        async def fake_get_async_db():
            yield PartiallyBrokenSession()

        app.dependency_overrides[get_async_db] = fake_get_async_db
        res = self.client.post("/api/v1/integrations/add-by-tmdb", json={"tmdb_id": 99})
        # The MetadataCache update is wrapped in its own try/except specifically so a
        # failure there doesn't break the overall request - the add itself still succeeds.
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])


class TestUpcomingMoviesStreamingDetection(unittest.TestCase):
    def setUp(self):
        from media_manager.web.api.auth import get_current_user, get_admin_user
        app.dependency_overrides[get_current_user] = lambda: _admin_user()
        app.dependency_overrides[get_admin_user] = lambda: _admin_user()
        self.client = TestClient(app)
        integrations_module.IN_MEMORY_UPCOMING_DISCOVER_CACHE.clear()
        integrations_module.IN_MEMORY_RADARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_CAST_CACHE.clear()
        integrations_module.IN_MEMORY_RELEASE_INFO_CACHE.clear()

    def tearDown(self):
        app.dependency_overrides.clear()
        integrations_module.IN_MEMORY_UPCOMING_DISCOVER_CACHE.clear()
        integrations_module.IN_MEMORY_RADARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_CAST_CACHE.clear()
        integrations_module.IN_MEMORY_RELEASE_INFO_CACHE.clear()

    @patch("media_manager.web.api.integrations._get_release_info_batch")
    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_streaming_release_flagged_with_service_name(self, mock_tmdb_cls, mock_release_batch):
        client_instance = mock_tmdb_cls.return_value
        client_instance.discover_upcoming_movies = AsyncMock(return_value=[
            {
                "id": 20, "title": "Streaming Movie", "release_date": "2026-08-05",
                "overview": "desc", "poster_path": "/p.jpg", "genre_ids": [], "popularity": 15.0
            }
        ])
        client_instance.get_movie_genres = AsyncMock(return_value={})
        client_instance.get_movie_details = AsyncMock(return_value={"credits": {"cast": []}})
        client_instance.extract_trailer_url = MagicMock(return_value=None)

        mock_release_batch.return_value = {
            20: [{"type": 4, "release_date": "2026-08-05T00:00:00.000Z", "note": "Netflix"}]
        }

        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(tmdb_config=_fake_config("tmdb"))
        res = self.client.get("/api/v1/integrations/upcoming-movies?days=30")

        self.assertEqual(res.status_code, 200)
        movie = res.json()["movies"][0]
        self.assertEqual(movie["release_medium"], "Streaming")
        self.assertIn("Netflix", movie["release_location"])

    @patch("media_manager.web.api.integrations._get_release_info_batch")
    @patch("media_manager.web.api.integrations.TMDbClient")
    def test_theatrical_release_in_window_preferred(self, mock_tmdb_cls, mock_release_batch):
        client_instance = mock_tmdb_cls.return_value
        client_instance.discover_upcoming_movies = AsyncMock(return_value=[
            {
                "id": 21, "title": "Theater Movie", "release_date": "2026-08-01",
                "overview": "desc", "poster_path": "/p.jpg", "genre_ids": [], "popularity": 15.0
            }
        ])
        client_instance.get_movie_genres = AsyncMock(return_value={})
        client_instance.get_movie_details = AsyncMock(return_value={"credits": {"cast": []}})
        client_instance.extract_trailer_url = MagicMock(return_value=None)

        mock_release_batch.return_value = {
            21: [{"type": 3, "release_date": "2026-08-10T00:00:00.000Z", "note": ""}]
        }

        app.dependency_overrides[get_async_db] = _make_upcoming_movies_db(tmdb_config=_fake_config("tmdb"))
        res = self.client.get("/api/v1/integrations/upcoming-movies?days=30")

        self.assertEqual(res.status_code, 200)
        movie = res.json()["movies"][0]
        self.assertEqual(movie["release_medium"], "Theater")
        self.assertEqual(movie["release_location"], "Theaters")
        self.assertEqual(movie["release_date"], "2026-08-10")


class TestHelperFunctionsDirect(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        integrations_module.IN_MEMORY_RADARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_SONARR_LIBRARY_CACHE.clear()
        integrations_module.IN_MEMORY_CAST_CACHE.clear()
        integrations_module.IN_MEMORY_RELEASE_INFO_CACHE.clear()
        integrations_module.IN_MEMORY_METADATA_CACHE.clear()

    async def test_get_radarr_tmdb_ids_not_configured_returns_empty_set(self):
        class FakeDb:
            async def execute(self, *a, **k):
                return _FakeQueryResult(scalar=None)

        result = await _get_radarr_tmdb_ids(FakeDb())
        self.assertEqual(result, set())

    @patch("media_manager.web.api.integrations.RadarrClient")
    async def test_get_radarr_tmdb_ids_uses_cache_on_second_call(self, mock_radarr_cls):
        instance = mock_radarr_cls.return_value
        instance.get_movies = AsyncMock(return_value=[{"tmdbId": 1}, {"tmdbId": 2}, {}])

        class FakeDb:
            async def execute(self, *a, **k):
                return _FakeQueryResult(scalar=_fake_config("radarr"))

        first = await _get_radarr_tmdb_ids(FakeDb())
        second = await _get_radarr_tmdb_ids(FakeDb())
        self.assertEqual(first, {1, 2})
        self.assertEqual(second, {1, 2})
        instance.get_movies.assert_awaited_once()

    async def test_get_sonarr_tvdb_ids_not_configured(self):
        class FakeDb:
            async def execute(self, *a, **k):
                return _FakeQueryResult(scalar=None)

        result = await _get_sonarr_tvdb_ids(FakeDb())
        self.assertEqual(result, set())

    @patch("media_manager.web.api.integrations.SonarrClient")
    async def test_get_sonarr_tvdb_ids_configured(self, mock_sonarr_cls):
        instance = mock_sonarr_cls.return_value
        instance.get_series = AsyncMock(return_value=[{"tvdbId": 55}])

        class FakeDb:
            async def execute(self, *a, **k):
                return _FakeQueryResult(scalar=_fake_config("sonarr"))

        result = await _get_sonarr_tvdb_ids(FakeDb())
        self.assertEqual(result, {55})

    async def test_get_cast_and_trailer_db_cache_hit(self):
        row = MovieCastCache(tmdb_id=7, cast_json='[{"name": "Actor"}]', trailer_url="https://yt/x")

        class FakeDb:
            async def execute(self, *a, **k):
                return _FakeQueryResult(scalars_list=[row])

        client = MagicMock()
        result = await _get_cast_and_trailer(client, [7], FakeDb())
        self.assertEqual(result[7]["trailer_url"], "https://yt/x")
        self.assertEqual(result[7]["cast"], [{"name": "Actor"}])

    async def test_get_cast_and_trailer_fetches_and_persists_on_full_miss(self):
        class FakeDb:
            def __init__(self):
                self.added = []
                self.committed = False

            async def execute(self, *a, **k):
                return _FakeQueryResult(scalars_list=[])

            def add(self, obj):
                self.added.append(obj)

            async def commit(self):
                self.committed = True

            async def rollback(self):
                pass

        client = MagicMock()
        client.get_movie_details = AsyncMock(return_value={
            "credits": {"cast": [{"id": 501, "name": "Star", "character": "Hero", "profile_path": "/x.jpg"}]}
        })
        client.get_person_details = AsyncMock(return_value={"biography": "A storied career."})
        client.extract_trailer_url = MagicMock(return_value="https://yt/z")

        db = FakeDb()
        result = await _get_cast_and_trailer(client, [8], db)
        self.assertEqual(result[8]["cast"][0]["name"], "Star")
        self.assertEqual(result[8]["cast"][0]["bio"], "A storied career.")
        self.assertEqual(result[8]["trailer_url"], "https://yt/z")
        client.get_person_details.assert_awaited_once_with(501)
        self.assertTrue(db.committed)
        self.assertEqual(len(db.added), 1)

    async def test_get_cast_and_trailer_bio_fetch_failure_leaves_empty_bio(self):
        class FakeDb:
            async def execute(self, *a, **k):
                return _FakeQueryResult(scalars_list=[])

            def add(self, obj):
                pass

            async def commit(self):
                pass

            async def rollback(self):
                pass

        client = MagicMock()
        client.get_movie_details = AsyncMock(return_value={
            "credits": {"cast": [{"id": 502, "name": "No Bio", "character": "Extra", "profile_path": None}]}
        })
        client.get_person_details = AsyncMock(return_value=None)
        client.extract_trailer_url = MagicMock(return_value=None)

        result = await _get_cast_and_trailer(client, [12], FakeDb())
        self.assertEqual(result[12]["cast"][0]["bio"], "")

    async def test_get_cast_and_trailer_skips_bio_fetch_when_no_person_id(self):
        class FakeDb:
            async def execute(self, *a, **k):
                return _FakeQueryResult(scalars_list=[])

            def add(self, obj):
                pass

            async def commit(self):
                pass

            async def rollback(self):
                pass

        client = MagicMock()
        client.get_movie_details = AsyncMock(return_value={
            "credits": {"cast": [{"name": "No Id", "character": "Extra", "profile_path": None}]}
        })
        client.get_person_details = AsyncMock(return_value={"biography": "Should not be called."})
        client.extract_trailer_url = MagicMock(return_value=None)

        result = await _get_cast_and_trailer(client, [13], FakeDb())
        self.assertEqual(result[13]["cast"][0]["bio"], "")
        client.get_person_details.assert_not_awaited()

    async def test_get_cast_and_trailer_include_bios_false_skips_person(self):
        class FakeDb:
            async def execute(self, *a, **k):
                return _FakeQueryResult(scalars_list=[])

            def add(self, obj):
                pass

            async def commit(self):
                pass

            async def rollback(self):
                pass

        client = MagicMock()
        client.get_movie_details = AsyncMock(return_value={
            "credits": {"cast": [{"id": 501, "name": "Star", "character": "Hero", "profile_path": None}]}
        })
        client.get_person_details = AsyncMock(return_value={"biography": "Should not be called."})
        client.extract_trailer_url = MagicMock(return_value=None)

        result = await _get_cast_and_trailer(client, [14], FakeDb(), include_bios=False)
        self.assertEqual(result[14]["cast"][0]["name"], "Star")
        client.get_person_details.assert_not_awaited()

    def test_limit_discover_results_caps_by_popularity(self):
        items = [{"id": i, "popularity": float(i)} for i in range(50)]
        out = integrations_module._limit_discover_results(items)
        self.assertEqual(len(out), integrations_module.DISCOVERY_RESULT_LIMIT)
        self.assertEqual(out[0]["id"], 49)

    async def test_get_cast_and_trailer_persist_failure_is_caught(self):
        class FakeDb:
            async def execute(self, *a, **k):
                return _FakeQueryResult(scalars_list=[])

            def add(self, obj):
                pass

            async def commit(self):
                raise RuntimeError("boom")

            async def rollback(self):
                pass

        client = MagicMock()
        client.get_movie_details = AsyncMock(return_value={"credits": {"cast": []}})
        client.extract_trailer_url = MagicMock(return_value=None)

        result = await _get_cast_and_trailer(client, [9], FakeDb())
        self.assertEqual(result[9]["cast"], [])

    async def test_get_cast_and_trailer_tv_media_type_uses_tv_details(self):
        class FakeDb:
            async def execute(self, *a, **k):
                return _FakeQueryResult(scalars_list=[])

            def add(self, obj):
                pass

            async def commit(self):
                pass

            async def rollback(self):
                pass

        client = MagicMock()
        client.get_tv_details = AsyncMock(return_value={"credits": {"cast": []}})
        client.extract_trailer_url = MagicMock(return_value=None)

        await _get_cast_and_trailer(client, [11], FakeDb(), media_type="tv")
        client.get_tv_details.assert_awaited_once_with(11)

    async def test_get_release_info_batch_uses_cache_then_fetches_missing(self):
        integrations_module.IN_MEMORY_RELEASE_INFO_CACHE[1] = [{"type": 3, "release_date": "2026-01-01"}]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [{"iso_3166_1": "US", "release_dates": [{"type": 2, "release_date": "2026-02-02"}]}]
        }
        mock_async_client = AsyncMock()
        mock_async_client.get = AsyncMock(return_value=mock_response)

        class _CM:
            async def __aenter__(self):
                return mock_async_client
            async def __aexit__(self, *a):
                return False

        class FakeClient:
            api_url = "https://api.example.invalid/3"
            headers = {}
            params = {}
            def _client(self, timeout=8.0):
                return _CM()

        result = await _get_release_info_batch(FakeClient(), [1, 2])

        self.assertEqual(result[1], [{"type": 3, "release_date": "2026-01-01"}])
        self.assertEqual(result[2][0]["type"], 2)

    async def test_get_release_info_batch_swallows_request_errors(self):
        mock_async_client = AsyncMock()
        mock_async_client.get = AsyncMock(side_effect=RuntimeError("network down"))

        class _CM:
            async def __aenter__(self):
                return mock_async_client
            async def __aexit__(self, *a):
                return False

        class FakeClient:
            api_url = "https://api.example.invalid/3"
            headers = {}
            params = {}
            def _client(self, timeout=8.0):
                return _CM()

        result = await _get_release_info_batch(FakeClient(), [3])

        self.assertEqual(result[3], [])

    async def test_populate_media_details_noop_on_empty_items(self):
        # The tmdb-config lookup happens before the `if not items: return` guard,
        # so a single execute() call is expected even for an empty item list.
        class FakeDb:
            async def execute(self, *a, **k):
                return _FakeQueryResult(scalar=None)

        await _populate_media_details([], FakeDb())

    async def test_populate_media_details_without_tmdb_client_leaves_items_unchanged(self):
        class FakeDb:
            async def execute(self, *a, **k):
                return _FakeQueryResult(scalar=None, scalars_list=[])

        items = [{"title": "Some Movie", "media_type": "movie"}]
        await _populate_media_details(items, FakeDb())
        self.assertNotIn("overview", items[0])

    async def test_populate_media_details_tolerates_corrupt_cache_json(self):
        integrations_module.IN_MEMORY_METADATA_CACHE.clear()
        integrations_module.IN_MEMORY_CAST_CACHE.clear()

        meta_row = type("MetaRow", (), {})()
        meta_row.cache_key = "movie:some movie"
        meta_row.tmdb_id = 42
        meta_row.overview = "ok"
        meta_row.rating = 7.1
        meta_row.genres_json = "{not-valid-json"
        meta_row.poster_path = "/p.jpg"
        meta_row.backdrop_path = None

        cast_row = type("CastRow", (), {})()
        cast_row.tmdb_id = 42
        cast_row.cast_json = "not-json"
        cast_row.trailer_url = None

        class FakeDb:
            async def execute(self, statement, *a, **k):
                compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
                if "integration_configs" in compiled:
                    return _FakeQueryResult(scalar=None)
                if "app_log_events" in compiled:
                    return _FakeQueryResult(scalars_list=[])
                if "metadata_cache" in compiled:
                    return _FakeQueryResult(scalars_list=[meta_row])
                if "movie_cast_cache" in compiled:
                    return _FakeQueryResult(scalars_list=[cast_row])
                return _FakeQueryResult(scalar=None, scalars_list=[])

        items = [{"title": "Some Movie", "media_type": "movie"}]
        await _populate_media_details(items, FakeDb())
        self.assertEqual(items[0].get("genres"), [])
        self.assertEqual(items[0].get("cast"), [])
        self.assertEqual(items[0].get("poster_url"), "/p.jpg")


if __name__ == "__main__":
    unittest.main()
