import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from media_manager.web.app import app
from media_manager.web.api.auth import get_current_user, get_admin_user, get_current_active_user
from media_manager.database import get_async_db
from media_manager.web.api import integrations as integrations_module
from tests.test_api_integrations import _fake_config, _FakeQueryResult


def _event(username, tmdb_id=None, tvdb_id=None, title="A Title", media_type="movie", created_at=None):
    ctx = {"username": username, "title": title}
    if tmdb_id is not None:
        ctx["tmdb_id"] = tmdb_id
    if tvdb_id is not None:
        ctx["tvdb_id"] = tvdb_id
    ev = type("Ev", (), {})()
    ev.event_tag = "[MEDIA_REQUEST]"
    ev.message = f"User '{username}' requested {'movie' if media_type == 'movie' else 'TV show'} '{title}'"
    ev.context_json = json.dumps(ctx)
    ev.created_at = created_at or datetime.utcnow()
    return ev


def _routing_db(configs=None, log_events=None, local_titles=None):
    """
    Fake async DB session for /my-requests: dispatches integration_configs,
    app_log_events, and media_items(title, media_type) queries.
    """
    configs = configs or {}
    log_events = log_events or []
    local_titles = local_titles or []  # list of (title, media_type) tuples

    class FakeSession:
        async def execute(self, statement, *args, **kwargs):
            compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
            if "integration_configs" in compiled:
                for name, cfg in configs.items():
                    if f"'{name}'" in compiled:
                        return _FakeQueryResult(scalar=cfg)
                return _FakeQueryResult(scalar=None)
            if "app_log_events" in compiled:
                return _FakeQueryResult(scalars_list=log_events)
            if "media_items" in compiled:
                return _FakeQueryResult(rows=local_titles)
            if "movie_cast_cache" in compiled or "metadata_cache" in compiled:
                return _FakeQueryResult(scalars_list=[])
            raise AssertionError(f"Unexpected query: {compiled}")

        def add(self, *a, **k):
            pass

        async def commit(self):
            pass

        async def rollback(self):
            pass

    async def fake_get_async_db():
        yield FakeSession()

    return fake_get_async_db


class TestMyRequests(unittest.TestCase):
    def setUp(self):
        admin_user = {"username": "alice", "role": "user"}
        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_admin_user] = lambda: admin_user
        app.dependency_overrides[get_current_active_user] = lambda: admin_user
        self.client = TestClient(app)
        integrations_module.IN_MEMORY_CAST_CACHE.clear()
        integrations_module.IN_MEMORY_TV_WATCH_PROVIDERS_CACHE.clear()
        integrations_module.IN_MEMORY_MOVIE_WATCH_PROVIDERS_CACHE.clear()

    def tearDown(self):
        app.dependency_overrides.clear()
        integrations_module.IN_MEMORY_CAST_CACHE.clear()
        integrations_module.IN_MEMORY_TV_WATCH_PROVIDERS_CACHE.clear()
        integrations_module.IN_MEMORY_MOVIE_WATCH_PROVIDERS_CACHE.clear()

    def test_no_requests_returns_empty(self):
        app.dependency_overrides[get_async_db] = _routing_db()
        res = self.client.get("/api/v1/integrations/my-requests")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"count": 0, "requests": []})

    def test_only_returns_current_users_requests(self):
        events = [
            _event("alice", tmdb_id=1, title="Alice's Movie"),
            _event("bob", tmdb_id=2, title="Bob's Movie"),
        ]
        app.dependency_overrides[get_async_db] = _routing_db(log_events=events)
        res = self.client.get("/api/v1/integrations/my-requests")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["requests"][0]["title"], "Alice's Movie")

    def test_status_available_via_local_library(self):
        events = [_event("alice", tmdb_id=1, title="Already Owned")]
        app.dependency_overrides[get_async_db] = _routing_db(
            log_events=events, local_titles=[("Already Owned", "movie")]
        )
        res = self.client.get("/api/v1/integrations/my-requests")
        self.assertEqual(res.json()["requests"][0]["status"], "available")

    def test_status_available_via_radarr_has_file(self):
        events = [_event("alice", tmdb_id=1, title="Downloaded Movie")]
        configs = {"radarr": _fake_config("radarr")}
        app.dependency_overrides[get_async_db] = _routing_db(configs=configs, log_events=events)
        with patch.object(integrations_module, "RadarrClient") as MockRadarr:
            MockRadarr.return_value.get_movies = AsyncMock(return_value=[{"id": 10, "tmdbId": 1, "hasFile": True}])
            MockRadarr.return_value.get_queue = AsyncMock(return_value=[])
            res = self.client.get("/api/v1/integrations/my-requests")
        self.assertEqual(res.json()["requests"][0]["status"], "available")

    def test_status_downloading_via_radarr_queue(self):
        events = [_event("alice", tmdb_id=1, title="Queued Movie")]
        configs = {"radarr": _fake_config("radarr")}
        app.dependency_overrides[get_async_db] = _routing_db(configs=configs, log_events=events)
        with patch.object(integrations_module, "RadarrClient") as MockRadarr:
            MockRadarr.return_value.get_movies = AsyncMock(return_value=[{"id": 10, "tmdbId": 1, "hasFile": False}])
            MockRadarr.return_value.get_queue = AsyncMock(return_value=[{"movieId": 10}])
            res = self.client.get("/api/v1/integrations/my-requests")
        self.assertEqual(res.json()["requests"][0]["status"], "downloading")

    def test_status_requested_when_neither_available_nor_downloading(self):
        events = [_event("alice", tmdb_id=1, title="Pending Movie")]
        configs = {"radarr": _fake_config("radarr")}
        app.dependency_overrides[get_async_db] = _routing_db(configs=configs, log_events=events)
        with patch.object(integrations_module, "RadarrClient") as MockRadarr:
            MockRadarr.return_value.get_movies = AsyncMock(return_value=[])
            MockRadarr.return_value.get_queue = AsyncMock(return_value=[])
            res = self.client.get("/api/v1/integrations/my-requests")
        self.assertEqual(res.json()["requests"][0]["status"], "requested")

    def test_tv_status_available_via_sonarr_episode_file_count(self):
        events = [_event("alice", tvdb_id=5, title="Downloaded Show", media_type="tv")]
        configs = {"sonarr": _fake_config("sonarr")}
        app.dependency_overrides[get_async_db] = _routing_db(configs=configs, log_events=events)
        with patch.object(integrations_module, "SonarrClient") as MockSonarr:
            MockSonarr.return_value.get_series = AsyncMock(return_value=[
                {"id": 20, "tvdbId": 5, "statistics": {"episodeFileCount": 3}}
            ])
            MockSonarr.return_value.get_queue = AsyncMock(return_value=[])
            res = self.client.get("/api/v1/integrations/my-requests")
        self.assertEqual(res.json()["requests"][0]["status"], "available")

    def test_dedupes_by_tmdb_id_keeping_most_recent(self):
        old_ev = _event("alice", tmdb_id=1, title="Same Movie", created_at=datetime.utcnow() - timedelta(days=5))
        new_ev = _event("alice", tmdb_id=1, title="Same Movie", created_at=datetime.utcnow())
        # Events arrive newest-first, matching the endpoint's own ORDER BY created_at DESC.
        app.dependency_overrides[get_async_db] = _routing_db(log_events=[new_ev, old_ev])
        res = self.client.get("/api/v1/integrations/my-requests")
        data = res.json()
        self.assertEqual(data["count"], 1)

    def test_falls_back_to_message_regex_when_no_context_json(self):
        ev = type("Ev", (), {})()
        ev.event_tag = "[MEDIA_REQUEST]"
        ev.message = "User 'alice' requested movie 'Regex Parsed Movie' (TMDb: 42) via Radarr"
        ev.context_json = None
        ev.created_at = datetime.utcnow()
        app.dependency_overrides[get_async_db] = _routing_db(log_events=[ev])
        res = self.client.get("/api/v1/integrations/my-requests")
        data = res.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["requests"][0]["title"], "Regex Parsed Movie")
        self.assertEqual(data["requests"][0]["tmdb_id"], 42)

    def test_client_errors_are_logged_and_swallowed(self):
        events = [_event("alice", tmdb_id=1, title="A Movie")]
        configs = {"radarr": _fake_config("radarr")}
        app.dependency_overrides[get_async_db] = _routing_db(configs=configs, log_events=events)
        with patch.object(integrations_module, "RadarrClient") as MockRadarr:
            MockRadarr.return_value.get_movies = AsyncMock(side_effect=Exception("radarr down"))
            res = self.client.get("/api/v1/integrations/my-requests")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["requests"][0]["status"], "requested")

    def test_enriched_with_poster_overview_cast_when_tmdb_configured(self):
        events = [_event("alice", tmdb_id=1, title="Movie With Details")]
        configs = {"tmdb": _fake_config("tmdb")}
        app.dependency_overrides[get_async_db] = _routing_db(configs=configs, log_events=events)
        with patch.object(integrations_module, "TMDbClient") as MockTMDb:
            instance = MockTMDb.return_value
            instance.get_movie_details = AsyncMock(return_value={
                "overview": "A great overview.", "poster_path": "/p1.jpg", "backdrop_path": "/b1.jpg",
                "vote_average": 8.2, "genres": [{"id": 28, "name": "Action"}],
                "credits": {"cast": [{"id": 1, "name": "Star", "character": "Hero", "profile_path": None}]},
                "videos": {"results": [{"site": "YouTube", "type": "Trailer", "official": True, "key": "abc"}]}
            })
            instance.get_person_details = AsyncMock(return_value={"biography": "Bio."})
            instance.get_movie_watch_providers = AsyncMock(return_value=["Netflix"])
            # extract_trailer_url is a staticmethod; wire up the real implementation
            # since the whole TMDbClient class is mocked above.
            from media_manager.integrations.tmdb import TMDbClient
            instance.extract_trailer_url = TMDbClient.extract_trailer_url
            res = self.client.get("/api/v1/integrations/my-requests")

        self.assertEqual(res.status_code, 200)
        item = res.json()["requests"][0]
        self.assertEqual(item["overview"], "A great overview.")
        self.assertTrue(item["poster_url"].endswith("/p1.jpg"))
        self.assertEqual(item["rating"], 8.2)
        self.assertEqual(item["genres"], ["Action"])
        self.assertEqual(item["cast"][0]["name"], "Star")
        self.assertEqual(item["trailer_url"], "https://www.youtube.com/watch?v=abc")
        self.assertEqual(item["streaming_providers"], ["Netflix"])

    def test_no_enrichment_fields_are_blank_when_tmdb_not_configured(self):
        events = [_event("alice", tmdb_id=1, title="No TMDb Movie")]
        app.dependency_overrides[get_async_db] = _routing_db(log_events=events)
        res = self.client.get("/api/v1/integrations/my-requests")
        item = res.json()["requests"][0]
        self.assertIsNone(item["overview"])
        self.assertIsNone(item["poster_url"])
        self.assertEqual(item["genres"], [])
        self.assertEqual(item["cast"], [])


if __name__ == "__main__":
    unittest.main()
