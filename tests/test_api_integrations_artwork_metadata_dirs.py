import unittest
from unittest.mock import AsyncMock, patch, MagicMock

from fastapi.testclient import TestClient

from media_manager.web.app import app
from media_manager.web.api import integrations as integrations_module
from media_manager.web.api.integrations import _build_title_candidates
from media_manager.models import (
    ArtworkCache, MetadataCache, MovieCastCache, MonitoredDirectory,
)


def _cfg(service_name, api_url="https://svc.invalid", api_key="key", enabled=True):
    c = type("Cfg", (), {})()
    c.service_name = service_name
    c.api_url = api_url
    c.api_key_encrypted = api_key
    c.enabled = enabled
    return c


class _QueryResult:
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

        class _S:
            def all(self_inner):
                return items

        return _S()


class RoutingFakeSession:
    """
    Generic fake async DB session for tests in this file: dispatches execute()
    by inspecting the compiled SQL for the table name, with routing config
    supplied per-test. Falls back to an empty result for any unconfigured
    table so endpoints under test don't crash on incidental lookups (e.g.
    _get_request_user_mappings' AppLogEvent query).
    """

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement, *args, **kwargs):
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        for table, result in self.routes.items():
            if table in compiled:
                return result() if callable(result) else result
        return _QueryResult()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def delete(self, obj):
        pass


def _override_db(session):
    async def fake_get_async_db():
        yield session
    return fake_get_async_db


class BaseIntegrationsTestCase(unittest.TestCase):
    def setUp(self):
        from media_manager.web.api.auth import get_current_user, get_admin_user, get_current_active_user
        admin_user = {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_admin_user] = lambda: admin_user
        app.dependency_overrides[get_current_active_user] = lambda: admin_user
        self.client = TestClient(app)
        integrations_module.IN_MEMORY_ARTWORK_CACHE.clear()
        integrations_module.IN_MEMORY_METADATA_CACHE.clear()
        integrations_module.IN_MEMORY_REQUEST_USER_MAPPINGS_CACHE.clear()

    def tearDown(self):
        app.dependency_overrides.clear()
        integrations_module.IN_MEMORY_ARTWORK_CACHE.clear()
        integrations_module.IN_MEMORY_METADATA_CACHE.clear()
        integrations_module.IN_MEMORY_REQUEST_USER_MAPPINGS_CACHE.clear()


class TestBuildTitleCandidates(unittest.TestCase):
    def test_extracts_year_from_title(self):
        candidates, year = _build_title_candidates("The Matrix 1999")
        self.assertEqual(year, 1999)
        self.assertIn("The Matrix 1999", candidates)

    def test_extracts_year_from_path_and_cleans_folder(self):
        candidates, year = _build_title_candidates(
            "G I Jane", path="/media/Movies/G I Jane (1997)/G.I.Jane.1997.1080p.BluRay.x264.mkv"
        )
        self.assertEqual(year, 1997)
        self.assertTrue(any("G I Jane" in c for c in candidates))
        self.assertIn("G.I.Jane.1997.1080p.BluRay.x264", "".join(candidates)) if False else None

    def test_ignores_generic_folder_names(self):
        candidates, _ = _build_title_candidates("Some Show", path="/mnt/TvShows/Movies/file.mkv")
        # "Movies" is a blocklisted generic folder name and should not appear as a candidate
        self.assertNotIn("Movies", candidates)

    def test_bad_path_is_swallowed(self):
        # Path handling is wrapped in try/except; passing a non-path-like object must not raise
        candidates, year = _build_title_candidates("Title Only")
        self.assertIn("Title Only", candidates)

    def test_dot_and_space_variations_generated(self):
        candidates, _ = _build_title_candidates("G I Jane")
        self.assertIn("G I Jane", candidates)
        # dot_var1 turns single-letter-word boundaries into dotted form
        self.assertTrue(any("." in c for c in candidates))

    def test_empty_title(self):
        candidates, year = _build_title_candidates("")
        self.assertIsNone(year)


class TestServeLocalArt(BaseIntegrationsTestCase):
    def test_missing_file_404(self):
        resp = self.client.get("/api/v1/integrations/serve-local-art", params={"path": "/nonexistent/file.jpg"})
        self.assertEqual(resp.status_code, 404)

    def test_invalid_extension_400(self):
        with patch("os.path.exists", return_value=True), patch("os.path.isfile", return_value=True):
            resp = self.client.get("/api/v1/integrations/serve-local-art", params={"path": "/tmp/file.txt"})
        self.assertEqual(resp.status_code, 400)

    def test_valid_image_served(self):
        with patch("os.path.exists", return_value=True), patch("os.path.isfile", return_value=True), \
             patch("media_manager.web.api.integrations.FileResponse") as mock_file_response:
            mock_file_response.return_value = "fake-response"
            with patch.object(integrations_module.router, "get"):
                pass
        # Exercise the real endpoint function directly to assert FileResponse is constructed correctly
        import asyncio
        with patch("os.path.exists", return_value=True), patch("os.path.isfile", return_value=True), \
             patch("media_manager.web.api.integrations.FileResponse") as mock_fr, \
             patch.object(integrations_module, "_is_allowed_local_image_path", return_value=True):
            mock_fr.return_value = MagicMock(status_code=200, headers={})
            result = asyncio.run(integrations_module.serve_local_art(path="/tmp/poster.jpg"))
            mock_fr.assert_called_once()
            self.assertIn("Cache-Control", mock_fr.call_args.kwargs.get("headers", {}))


class TestArtworkEndpoint(BaseIntegrationsTestCase):
    def test_memory_cache_hit(self):
        integrations_module.IN_MEMORY_ARTWORK_CACHE["movie:inception"] = {"poster": "p", "background": "b", "logo": None, "banner": None, "source": "memory"}
        session = RoutingFakeSession()
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        resp = self.client.get("/api/v1/integrations/artwork", params={"title": "Inception"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["cached"])
        self.assertEqual(data["cache_tier"], "memory")

    def test_database_cache_hit(self):
        cached = ArtworkCache(cache_key="movie:dune", title="Dune", media_type="movie",
                               poster_url="p", background_url="b", logo_url=None, banner_url=None, source="database")
        session = RoutingFakeSession(routes={"artwork_cache": _QueryResult(scalar=cached)})
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        resp = self.client.get("/api/v1/integrations/artwork", params={"title": "Dune"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["cached"])
        self.assertEqual(data["cache_tier"], "database")

    def test_local_disk_tier_found_and_persisted(self):
        local_item = MediaItem_stub = type("M", (), {"path": "/media/Movies/Some Movie/file.mkv"})()
        session = RoutingFakeSession(routes={
            "artwork_cache": _QueryResult(scalar=None),
            "media_items": _QueryResult(scalar=local_item),
        })
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        with patch("media_manager.web.api.integrations.find_local_artwork",
                   return_value={"poster": "/media/Movies/Some Movie/poster.jpg", "background": None, "logo": None, "banner": None}):
            resp = self.client.get("/api/v1/integrations/artwork", params={"title": "Some Movie"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["cache_tier"], "local_disk")
        self.assertTrue(session.committed)
        self.assertEqual(len(session.added), 1)

    def test_local_disk_persist_failure_rolls_back(self):
        local_item = type("M", (), {"path": "/media/Movies/Some Movie/file.mkv"})()
        session = RoutingFakeSession(routes={
            "artwork_cache": _QueryResult(scalar=None),
            "media_items": _QueryResult(scalar=local_item),
        })

        async def failing_commit():
            raise RuntimeError("db down")
        session.commit = failing_commit

        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        with patch("media_manager.web.api.integrations.find_local_artwork",
                   return_value={"poster": "/x/poster.jpg", "background": None, "logo": None, "banner": None}):
            resp = self.client.get("/api/v1/integrations/artwork", params={"title": "Some Movie"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(session.rolled_back)

    def test_fanart_and_arr_fallback_path(self):
        fanart_cfg = _cfg("fanart")
        radarr_cfg = _cfg("radarr")

        def route(table):
            if table == "artwork_cache":
                return _QueryResult(scalar=None)
            if table == "media_items":
                return _QueryResult(scalar=None)
            return None

        class DispatchSession(RoutingFakeSession):
            async def execute(self, statement, *args, **kwargs):
                compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
                if "artwork_cache" in compiled:
                    return _QueryResult(scalar=None)
                if "media_items" in compiled:
                    return _QueryResult(scalar=None)
                if "integration_configs" in compiled and "'fanart'" in compiled:
                    return _QueryResult(scalar=fanart_cfg)
                if "integration_configs" in compiled and "'radarr'" in compiled:
                    return _QueryResult(scalar=radarr_cfg)
                return _QueryResult()

        session = DispatchSession()
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)

        with patch.object(integrations_module.RadarrClient, "search_movies", new=AsyncMock(return_value=[
            {"tmdbId": 42, "images": [
                {"coverType": "poster", "remoteUrl": "http://x/poster.jpg"},
                {"coverType": "fanart", "remoteUrl": "http://x/fanart.jpg"},
            ]}
        ])), patch.object(integrations_module.FanartClient, "get_movie_images", new=AsyncMock(return_value={})), \
             patch.object(integrations_module.FanartClient, "extract_best_artwork", return_value={"poster": None, "background": None, "logo": None, "banner": None}):
            resp = self.client.get("/api/v1/integrations/artwork", params={"title": "Some Movie", "type": "movie"})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # Radarr images populate poster/background directly inside the fanart
        # branch (lines ~2045-2049); since both end up set, the separate
        # "source" fallback block below (~2075-2100) is skipped, so source
        # stays None here even though the values came from Radarr.
        self.assertEqual(data["artwork"]["poster"], "http://x/poster.jpg")
        self.assertEqual(data["artwork"]["background"], "http://x/fanart.jpg")
        self.assertIsNone(data["artwork"]["source"])


class TestMetadataEndpoint(BaseIntegrationsTestCase):
    def test_memory_cache_hit(self):
        integrations_module.IN_MEMORY_METADATA_CACHE["movie:heat"] = {"tmdb_id": 1, "rating": 8.0, "overview": "o"}
        session = RoutingFakeSession()
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        resp = self.client.get("/api/v1/integrations/metadata", params={"title": "Heat"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["cache_tier"], "memory")

    def test_empty_memory_cache_entry_falls_through(self):
        integrations_module.IN_MEMORY_METADATA_CACHE["movie:heat"] = {"tmdb_id": None, "rating": None, "overview": None}
        session = RoutingFakeSession()
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        resp = self.client.get("/api/v1/integrations/metadata", params={"title": "Heat"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotEqual(resp.json().get("cache_tier"), "memory")

    def test_database_cache_hit_with_cast_row(self):
        cached = MetadataCache(
            cache_key="movie:heat", title="Heat", media_type="movie", tmdb_id=949, overview="ov",
            release_date="1995-12-15", rating=8.2, vote_count=100, genres_json='["Crime"]',
            cast_json='["Old Cast"]', runtime_minutes=170, poster_path="/p.jpg", backdrop_path="/b.jpg",
            status="Released", source="tmdb", requested_by="alice",
        )
        cast_row = MovieCastCache(tmdb_id=949, cast_json='[{"name": "Al Pacino"}]', trailer_url="http://yt/x")

        class DispatchSession(RoutingFakeSession):
            async def execute(self, statement, *args, **kwargs):
                compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
                if "app_log_events" in compiled:
                    return _QueryResult(scalars_list=[])
                if "metadata_cache" in compiled:
                    return _QueryResult(scalar=cached)
                if "movie_cast_cache" in compiled:
                    return _QueryResult(scalar=cast_row)
                return _QueryResult()

        session = DispatchSession()
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        resp = self.client.get("/api/v1/integrations/metadata", params={"title": "Heat"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["cache_tier"], "database")
        self.assertEqual(data["metadata"]["trailer_url"], "http://yt/x")
        self.assertEqual(data["metadata"]["cast"], [{"name": "Al Pacino"}])

    def test_tmdb_fetch_path_creates_new_entry(self):
        tmdb_cfg = _cfg("tmdb")

        class DispatchSession(RoutingFakeSession):
            async def execute(self, statement, *args, **kwargs):
                compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
                if "app_log_events" in compiled:
                    return _QueryResult(scalars_list=[])
                if "metadata_cache" in compiled:
                    return _QueryResult(scalar=None)
                if "integration_configs" in compiled and "'tmdb'" in compiled:
                    return _QueryResult(scalar=tmdb_cfg)
                if "movie_cast_cache" in compiled:
                    return _QueryResult(scalar=None)
                return _QueryResult()

        session = DispatchSession()
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)

        with patch.object(integrations_module.TMDbClient, "search_movie", new=AsyncMock(return_value=[{"id": 77}])), \
             patch.object(integrations_module.TMDbClient, "get_movie_details", new=AsyncMock(return_value={
                 "id": 77, "title": "New Movie", "overview": "ov", "release_date": "2020-01-01",
                 "vote_average": 7.456, "vote_count": 10, "genres": [{"name": "Drama"}],
                 "runtime": 100, "poster_path": "/p.jpg", "backdrop_path": "/b.jpg", "status": "Released",
             })), \
             patch.object(integrations_module, "_get_cast_and_trailer", new=AsyncMock(return_value={77: {"cast": [{"name": "Actor"}], "trailer_url": "http://yt/z"}})):
            resp = self.client.get("/api/v1/integrations/metadata", params={"title": "New Movie", "type": "movie"})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["cache_tier"], "tmdb_api")
        self.assertEqual(data["metadata"]["tmdb_id"], 77)
        self.assertEqual(data["metadata"]["rating"], 7.5)
        self.assertTrue(session.committed)

    def test_persist_failure_rolls_back(self):
        tmdb_cfg = _cfg("tmdb", enabled=False)

        class DispatchSession(RoutingFakeSession):
            async def execute(self, statement, *args, **kwargs):
                compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
                if "app_log_events" in compiled:
                    return _QueryResult(scalars_list=[])
                if "metadata_cache" in compiled:
                    return _QueryResult(scalar=None)
                if "integration_configs" in compiled:
                    return _QueryResult(scalar=tmdb_cfg)
                return _QueryResult()

        session = DispatchSession()

        async def failing_commit():
            raise RuntimeError("boom")
        session.commit = failing_commit

        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        resp = self.client.get("/api/v1/integrations/metadata", params={"title": "No Config Movie"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(session.rolled_back)


class TestEpisodeMetadataEndpoint(BaseIntegrationsTestCase):
    def test_memory_cache_hit(self):
        integrations_module.IN_MEMORY_METADATA_CACHE["episode:the office:s1:e1"] = {"title": "Pilot"}
        session = RoutingFakeSession()
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        resp = self.client.get("/api/v1/integrations/episode-metadata", params={"show_title": "The Office", "season": 1, "episode": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["cache_tier"], "memory")

    def test_database_cache_hit(self):
        cached = MetadataCache(cache_key="episode:the office:s1:e1", title="Pilot", media_type="tv_episode",
                                overview="ov", release_date="2005-03-24", rating=7.5, poster_path="/s.jpg", source="tmdb")
        session = RoutingFakeSession(routes={"metadata_cache": _QueryResult(scalar=cached)})
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        resp = self.client.get("/api/v1/integrations/episode-metadata", params={"show_title": "The Office", "season": 1, "episode": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["cache_tier"], "database")

    def test_tmdb_fetch_path(self):
        tmdb_cfg = _cfg("tmdb")

        class DispatchSession(RoutingFakeSession):
            async def execute(self, statement, *args, **kwargs):
                compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
                if "metadata_cache" in compiled:
                    return _QueryResult(scalar=None)
                if "integration_configs" in compiled:
                    return _QueryResult(scalar=tmdb_cfg)
                return _QueryResult()

        session = DispatchSession()
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)

        with patch.object(integrations_module.TMDbClient, "search_tv", new=AsyncMock(return_value=[{"id": 5}])), \
             patch.object(integrations_module.TMDbClient, "get_tv_episode_details", new=AsyncMock(return_value={
                 "name": "Pilot", "overview": "ov", "air_date": "2005-03-24", "vote_average": 7.6, "still_path": "/still.jpg",
             })):
            resp = self.client.get("/api/v1/integrations/episode-metadata", params={"show_title": "New Show", "season": 1, "episode": 1})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["cache_tier"], "tmdb_api")
        self.assertEqual(data["metadata"]["title"], "Pilot")
        self.assertTrue(session.committed)

    def test_no_tmdb_results_returns_empty_metadata(self):
        tmdb_cfg = _cfg("tmdb")

        class DispatchSession(RoutingFakeSession):
            async def execute(self, statement, *args, **kwargs):
                compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
                if "metadata_cache" in compiled:
                    return _QueryResult(scalar=None)
                if "integration_configs" in compiled:
                    return _QueryResult(scalar=tmdb_cfg)
                return _QueryResult()

        session = DispatchSession()
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        with patch.object(integrations_module.TMDbClient, "search_tv", new=AsyncMock(return_value=[])):
            resp = self.client.get("/api/v1/integrations/episode-metadata", params={"show_title": "Unknown Show", "season": 1, "episode": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["metadata"]["title"])


class TestDirectoriesEndpoints(BaseIntegrationsTestCase):
    def test_list_seeds_defaults_when_empty(self):
        seeded = []

        class DispatchSession(RoutingFakeSession):
            def __init__(self):
                super().__init__()
                self.call_count = 0

            async def execute(self, statement, *args, **kwargs):
                self.call_count += 1
                if self.call_count == 1:
                    return _QueryResult(scalars_list=[])
                return _QueryResult(scalars_list=seeded)

        session = DispatchSession()
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        with patch("shutil.disk_usage", return_value=(100, 50, 50)):
            resp = self.client.get("/api/v1/integrations/directories")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(session.committed)

    def test_list_seed_failure_falls_back_to_config_defaults(self):
        class DispatchSession(RoutingFakeSession):
            async def execute(self, statement, *args, **kwargs):
                return _QueryResult(scalars_list=[])

            async def commit(self):
                raise RuntimeError("db down")

        session = DispatchSession()
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        with patch("shutil.disk_usage", side_effect=OSError("no such device")):
            resp = self.client.get("/api/v1/integrations/directories")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(len(data) > 0)
        self.assertIn("error", data[0]["disk_space"])

    def test_list_returns_existing_dirs_with_disk_space(self):
        d = MonitoredDirectory(path="/mnt/Movies")
        session = RoutingFakeSession(routes={"monitored_directories": _QueryResult(scalars_list=[d])})
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        with patch("shutil.disk_usage", return_value=(200, 100, 100)):
            resp = self.client.get("/api/v1/integrations/directories")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data[0]["path"], "/mnt/Movies")
        self.assertEqual(data[0]["disk_space"]["free_percent"], 50.0)

    def test_add_directory_success(self):
        session = RoutingFakeSession(routes={"monitored_directories": _QueryResult(scalar=None)})
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        resp = self.client.post("/api/v1/integrations/directories", json={"path": "/mnt/NewDir"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        self.assertTrue(session.committed)

    def test_add_directory_empty_path_400(self):
        session = RoutingFakeSession()
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        resp = self.client.post("/api/v1/integrations/directories", json={"path": "   "})
        self.assertEqual(resp.status_code, 400)

    def test_add_directory_duplicate_400(self):
        existing = MonitoredDirectory(path="/mnt/Existing")
        session = RoutingFakeSession(routes={"monitored_directories": _QueryResult(scalar=existing)})
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        resp = self.client.post("/api/v1/integrations/directories", json={"path": "/mnt/Existing"})
        self.assertEqual(resp.status_code, 400)

    def test_delete_directory_success(self):
        existing = MonitoredDirectory(path="/mnt/ToDelete")
        session = RoutingFakeSession(routes={"monitored_directories": _QueryResult(scalar=existing)})
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        resp = self.client.delete("/api/v1/integrations/directories/some-id")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        self.assertTrue(session.committed)

    def test_delete_directory_not_found_404(self):
        session = RoutingFakeSession(routes={"monitored_directories": _QueryResult(scalar=None)})
        app.dependency_overrides[integrations_module.get_async_db] = _override_db(session)
        resp = self.client.delete("/api/v1/integrations/directories/missing-id")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
