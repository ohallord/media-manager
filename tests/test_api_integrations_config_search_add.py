import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from media_manager.web.app import app
from media_manager.database import get_async_db


def _fake_config(service_name, enabled=True, api_url="https://example.invalid", api_key="fake-key"):
    config = type("Cfg", (), {})()
    config.service_name = service_name
    config.api_url = api_url
    config.api_key_encrypted = api_key
    config.enabled = enabled
    config.status = "HEALTHY"
    config.last_checked_at = None
    return config


class _FakeQueryResult:
    def __init__(self, scalar=None, scalars_list=None):
        self._scalar = scalar
        self._scalars_list = scalars_list if scalars_list is not None else []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        items = self._scalars_list

        class _Scalars:
            def all(self_inner):
                return items

        return _Scalars()


def _make_routing_db(configs_by_service=None, media_items=None, metadata_cache=None):
    """
    Fake async DB session that routes execute() calls by inspecting the
    compiled SQL, mirroring the pattern in tests/test_api_integrations.py's
    _make_upcoming_movies_db.
    """
    configs_by_service = configs_by_service or {}
    media_items = media_items or []

    class FakeSession:
        def __init__(self):
            self.added = []
            self.committed = False

        async def execute(self, statement, *args, **kwargs):
            compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
            if "integration_configs" in compiled:
                for svc, cfg in configs_by_service.items():
                    if f"'{svc}'" in compiled:
                        return _FakeQueryResult(scalar=cfg)
                if "WHERE" not in compiled:
                    return _FakeQueryResult(scalars_list=list(configs_by_service.values()))
                return _FakeQueryResult(scalar=None)
            if "metadata_cache" in compiled:
                return _FakeQueryResult(scalar=metadata_cache)
            if "media_items" in compiled:
                return _FakeQueryResult(scalars_list=media_items)
            if "app_log_events" in compiled:
                return _FakeQueryResult(scalars_list=[])
            raise AssertionError(f"Unexpected query in test: {compiled}")

        def add(self, obj):
            self.added.append(obj)

        async def commit(self):
            self.committed = True

        async def rollback(self):
            pass

    session = FakeSession()

    async def fake_get_async_db():
        yield session

    return fake_get_async_db, session


class _BaseIntegrationsTest(unittest.TestCase):
    def setUp(self):
        from media_manager.web.api.auth import get_current_user, get_current_active_user, get_admin_user
        user = {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_current_active_user] = lambda: user
        app.dependency_overrides[get_admin_user] = lambda: user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()


class TestGetIntegrationConfigs(_BaseIntegrationsTest):
    def test_returns_configs_list(self):
        cfg = _fake_config("radarr")
        cfg.id = 1
        fake_db, _ = _make_routing_db(configs_by_service={})

        class ListSession:
            async def execute(self, *a, **k):
                return _FakeQueryResult(scalars_list=[cfg])

        async def fake_db_list():
            yield ListSession()

        app.dependency_overrides[get_async_db] = fake_db_list
        response = self.client.get("/api/v1/integrations/config")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["configs"]), 1)
        self.assertEqual(data["configs"][0]["service_name"], "radarr")


class TestUpdateIntegrationConfig(_BaseIntegrationsTest):
    def test_invalid_service_name_rejected(self):
        fake_db, _ = _make_routing_db()
        app.dependency_overrides[get_async_db] = fake_db
        response = self.client.put(
            "/api/v1/integrations/config/not-a-real-service",
            json={"api_url": "http://x", "api_key": "k"},
        )
        self.assertEqual(response.status_code, 400)

    def test_creates_new_config_when_none_exists(self):
        fake_db, session = _make_routing_db(configs_by_service={"radarr": None})
        app.dependency_overrides[get_async_db] = fake_db
        response = self.client.put(
            "/api/v1/integrations/config/radarr",
            json={"api_url": "http://radarr.local", "api_key": "k", "enabled": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(session.committed)
        self.assertEqual(len(session.added), 1)

    def test_updates_existing_config(self):
        existing = _fake_config("sonarr")
        fake_db, session = _make_routing_db(configs_by_service={"sonarr": existing})
        app.dependency_overrides[get_async_db] = fake_db
        response = self.client.put(
            "/api/v1/integrations/config/sonarr",
            json={"api_url": "http://sonarr.local", "api_key": "newkey", "enabled": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(existing.api_key_encrypted, "newkey")
        self.assertFalse(existing.enabled)
        self.assertEqual(len(session.added), 0)

    def test_email_service_uses_default_url_when_missing(self):
        fake_db, session = _make_routing_db(configs_by_service={"email": None})
        app.dependency_overrides[get_async_db] = fake_db
        response = self.client.put(
            "/api/v1/integrations/config/email",
            json={"api_url": "", "api_key": "k"},
        )
        self.assertEqual(response.status_code, 200)


class TestConnectionTest(_BaseIntegrationsTest):
    def test_radarr_healthy(self):
        app.dependency_overrides[get_async_db] = _make_routing_db()[0]
        with patch("media_manager.web.api.integrations.RadarrClient") as MockClient:
            instance = MockClient.return_value
            instance.get_status = AsyncMock(return_value=True)
            instance.get_profiles = AsyncMock(return_value=[{"id": 1, "name": "HD-1080p"}])
            instance.get_root_folders = AsyncMock(return_value=[{"id": 1, "path": "/movies"}])
            response = self.client.post(
                "/api/v1/integrations/test/radarr",
                json={"api_url": "http://radarr", "api_key": "k"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["healthy"])
        self.assertEqual(data["profiles"][0]["name"], "HD-1080p")

    def test_radarr_unhealthy_skips_profile_lookup(self):
        app.dependency_overrides[get_async_db] = _make_routing_db()[0]
        with patch("media_manager.web.api.integrations.RadarrClient") as MockClient:
            instance = MockClient.return_value
            instance.get_status = AsyncMock(return_value=False)
            response = self.client.post(
                "/api/v1/integrations/test/radarr",
                json={"api_url": "http://radarr", "api_key": "k"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["healthy"])
        self.assertEqual(data["profiles"], [])

    def test_sonarr_healthy(self):
        app.dependency_overrides[get_async_db] = _make_routing_db()[0]
        with patch("media_manager.web.api.integrations.SonarrClient") as MockClient:
            instance = MockClient.return_value
            instance.get_status = AsyncMock(return_value=True)
            instance.get_profiles = AsyncMock(return_value=[])
            instance.get_root_folders = AsyncMock(return_value=[])
            response = self.client.post(
                "/api/v1/integrations/test/sonarr",
                json={"api_url": "http://sonarr", "api_key": "k"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service"], "sonarr")

    def test_sabnzbd_healthy(self):
        app.dependency_overrides[get_async_db] = _make_routing_db()[0]
        with patch("media_manager.web.api.integrations.SABnzbdClient") as MockClient:
            instance = MockClient.return_value
            instance.get_status = AsyncMock(return_value=True)
            response = self.client.post(
                "/api/v1/integrations/test/sabnzbd",
                json={"api_url": "http://sab", "api_key": "k"},
            )
        self.assertTrue(response.json()["healthy"])

    def test_syncthing_healthy(self):
        app.dependency_overrides[get_async_db] = _make_routing_db()[0]
        with patch("media_manager.web.api.integrations.SyncthingClient") as MockClient:
            instance = MockClient.return_value
            instance.get_status = AsyncMock(return_value=True)
            response = self.client.post(
                "/api/v1/integrations/test/syncthing",
                json={"api_url": "http://sync", "api_key": "k"},
            )
        self.assertTrue(response.json()["healthy"])

    def test_fanart_healthy(self):
        app.dependency_overrides[get_async_db] = _make_routing_db()[0]
        with patch("media_manager.web.api.integrations.FanartClient") as MockClient:
            instance = MockClient.return_value
            instance.get_status = AsyncMock(return_value=True)
            response = self.client.post(
                "/api/v1/integrations/test/fanart",
                json={"api_url": "", "api_key": "k"},
            )
        self.assertTrue(response.json()["healthy"])

    def test_tmdb_healthy(self):
        app.dependency_overrides[get_async_db] = _make_routing_db()[0]
        with patch("media_manager.web.api.integrations.TMDbClient") as MockClient:
            instance = MockClient.return_value
            instance.get_status = AsyncMock(return_value=True)
            response = self.client.post(
                "/api/v1/integrations/test/tmdb",
                json={"api_url": "", "api_key": "k"},
            )
        self.assertTrue(response.json()["healthy"])

    def test_email_invalid_format(self):
        app.dependency_overrides[get_async_db] = _make_routing_db()[0]
        response = self.client.post(
            "/api/v1/integrations/test/email",
            json={"api_url": "not-enough-parts", "api_key": "k"},
        )
        data = response.json()
        self.assertFalse(data["healthy"])
        self.assertIn("Invalid configuration", data["error"])

    def test_email_connect_success_with_tls(self):
        app.dependency_overrides[get_async_db] = _make_routing_db()[0]
        with patch("smtplib.SMTP") as MockSMTP:
            instance = MockSMTP.return_value
            response = self.client.post(
                "/api/v1/integrations/test/email",
                json={"api_url": "smtp.example.com:587:me@example.com:true", "api_key": "pw"},
            )
        self.assertTrue(response.json()["healthy"])
        instance.starttls.assert_called_once()

    def test_email_connect_success_without_tls(self):
        app.dependency_overrides[get_async_db] = _make_routing_db()[0]
        with patch("smtplib.SMTP") as MockSMTP:
            response = self.client.post(
                "/api/v1/integrations/test/email",
                json={"api_url": "smtp.example.com:25:me@example.com:false", "api_key": ""},
            )
        self.assertTrue(response.json()["healthy"])

    def test_email_connect_exception_returns_unhealthy(self):
        app.dependency_overrides[get_async_db] = _make_routing_db()[0]
        with patch("smtplib.SMTP", side_effect=OSError("connection refused")):
            response = self.client.post(
                "/api/v1/integrations/test/email",
                json={"api_url": "smtp.example.com:587:me@example.com:true", "api_key": "pw"},
            )
        data = response.json()
        self.assertFalse(data["healthy"])
        self.assertIn("connection refused", data["error"])

    def test_invalid_service_rejected(self):
        app.dependency_overrides[get_async_db] = _make_routing_db()[0]
        response = self.client.post(
            "/api/v1/integrations/test/not-a-service",
            json={"api_url": "", "api_key": ""},
        )
        self.assertEqual(response.status_code, 400)


class TestSearchMediaLocalAndRemote(_BaseIntegrationsTest):
    def _item(self, title="Example Movie", media_type="movie"):
        item = type("Item", (), {})()
        item.id = 1
        item.title = title
        item.path = f"/media/Movies/{title}/{title}.mkv"
        item.media_type = media_type
        item.resolution = "1080p"
        item.quality_score = 80
        item.size_bytes = 1_000_000_000
        item.season = None
        item.episode = None
        return item

    def test_search_too_short_term_rejected(self):
        fake_db, _ = _make_routing_db()
        app.dependency_overrides[get_async_db] = fake_db
        response = self.client.get("/api/v1/integrations/search", params={"term": "a"})
        self.assertEqual(response.status_code, 422)

    def test_search_local_only_no_remote_config(self):
        item = self._item()
        fake_db, _ = _make_routing_db(configs_by_service={"radarr": None}, media_items=[item])
        app.dependency_overrides[get_async_db] = fake_db
        response = self.client.get("/api/v1/integrations/search", params={"term": "Example"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["has_local_matches"])
        self.assertEqual(data["local_count"], 1)

    def test_search_tv_autodetected_from_season_term(self):
        fake_db, _ = _make_routing_db(configs_by_service={"sonarr": None}, media_items=[])
        app.dependency_overrides[get_async_db] = fake_db
        response = self.client.get("/api/v1/integrations/search", params={"term": "Show Season 2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["type"], "tv")

    def test_search_remote_results_excludes_local_matches(self):
        item = self._item(title="Already Local")
        radarr_cfg = _fake_config("radarr")
        fake_db, _ = _make_routing_db(configs_by_service={"radarr": radarr_cfg}, media_items=[item])
        app.dependency_overrides[get_async_db] = fake_db
        with patch("media_manager.web.api.integrations.RadarrClient") as MockClient:
            instance = MockClient.return_value
            instance.search_movies = AsyncMock(return_value=[
                {"title": "Already Local", "tmdbId": 1},
                {"title": "New Movie", "tmdbId": 2, "overview": "desc", "year": 2024},
            ])
            instance.get_movies = AsyncMock(return_value=[])
            response = self.client.get("/api/v1/integrations/search", params={"term": "Example"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        titles = [r["title"] for r in data["results"]]
        self.assertIn("New Movie", titles)

    def test_search_returns_json_when_remote_lookup_times_out(self):
        radarr_cfg = _fake_config("radarr")
        fake_db, _ = _make_routing_db(configs_by_service={"radarr": radarr_cfg}, media_items=[])
        app.dependency_overrides[get_async_db] = fake_db

        async def hang(_term):
            await asyncio.sleep(30)
            return []

        with patch("media_manager.web.api.integrations.RadarrClient") as MockClient, \
             patch("media_manager.web.api.integrations.SEARCH_REMOTE_TIMEOUT_SECONDS", 0.05), \
             patch("media_manager.web.api.integrations._get_radarr_tmdb_ids", new=AsyncMock(return_value=set())), \
             patch("media_manager.web.api.integrations._get_sonarr_tvdb_ids", new=AsyncMock(return_value=set())), \
             patch("media_manager.web.api.integrations._populate_media_details", new=AsyncMock()):
            instance = MockClient.return_value
            instance.search_movies = hang
            instance.get_movies = AsyncMock(return_value=[])
            response = self.client.get("/api/v1/integrations/search", params={"term": "batman"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json", response.headers.get("content-type", ""))
        data = response.json()
        self.assertEqual(data["term"], "batman")
        self.assertEqual(data["results"], [])

    def test_search_returns_json_when_enrichment_times_out(self):
        radarr_cfg = _fake_config("radarr")
        fake_db, _ = _make_routing_db(configs_by_service={"radarr": radarr_cfg}, media_items=[])
        app.dependency_overrides[get_async_db] = fake_db

        async def hang_enrich(*_args, **_kwargs):
            await asyncio.sleep(30)

        with patch("media_manager.web.api.integrations.RadarrClient") as MockClient, \
             patch("media_manager.web.api.integrations.SEARCH_ENRICH_TIMEOUT_SECONDS", 0.05), \
             patch("media_manager.web.api.integrations._get_radarr_tmdb_ids", new=AsyncMock(return_value=set())), \
             patch("media_manager.web.api.integrations._get_sonarr_tvdb_ids", new=AsyncMock(return_value=set())), \
             patch("media_manager.web.api.integrations._populate_media_details", new=hang_enrich):
            instance = MockClient.return_value
            instance.search_movies = AsyncMock(return_value=[
                {"title": "The Batman", "tmdbId": 414906, "overview": "desc", "year": 2022},
            ])
            instance.get_movies = AsyncMock(return_value=[])
            response = self.client.get("/api/v1/integrations/search", params={"term": "batman"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json", response.headers.get("content-type", ""))
        titles = [r["title"] for r in response.json()["results"]]
        self.assertIn("The Batman", titles)

    def test_search_returns_json_when_remote_lookup_raises(self):
        radarr_cfg = _fake_config("radarr")
        fake_db, _ = _make_routing_db(configs_by_service={"radarr": radarr_cfg}, media_items=[])
        app.dependency_overrides[get_async_db] = fake_db
        with patch("media_manager.web.api.integrations.RadarrClient") as MockClient, \
             patch("media_manager.web.api.integrations._get_radarr_tmdb_ids", new=AsyncMock(return_value=set())), \
             patch("media_manager.web.api.integrations._get_sonarr_tvdb_ids", new=AsyncMock(return_value=set())), \
             patch("media_manager.web.api.integrations._populate_media_details", new=AsyncMock()):
            instance = MockClient.return_value
            instance.search_movies = AsyncMock(side_effect=RuntimeError("radarr down"))
            response = self.client.get("/api/v1/integrations/search", params={"term": "batman"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])

    def test_search_tv_returns_json_when_remote_lookup_times_out(self):
        sonarr_cfg = _fake_config("sonarr")
        fake_db, _ = _make_routing_db(configs_by_service={"sonarr": sonarr_cfg}, media_items=[])
        app.dependency_overrides[get_async_db] = fake_db

        async def hang(_term):
            await asyncio.sleep(30)
            return []

        with patch("media_manager.web.api.integrations.SonarrClient") as MockClient, \
             patch("media_manager.web.api.integrations.SEARCH_REMOTE_TIMEOUT_SECONDS", 0.05), \
             patch("media_manager.web.api.integrations._get_radarr_tmdb_ids", new=AsyncMock(return_value=set())), \
             patch("media_manager.web.api.integrations._get_sonarr_tvdb_ids", new=AsyncMock(return_value=set())), \
             patch("media_manager.web.api.integrations._populate_media_details", new=AsyncMock()):
            instance = MockClient.return_value
            instance.search_series = hang
            response = self.client.get(
                "/api/v1/integrations/search",
                params={"term": "batman", "type": "tv"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["type"], "tv")
        self.assertEqual(response.json()["results"], [])

    def test_search_returns_json_when_library_id_lookup_times_out(self):
        radarr_cfg = _fake_config("radarr")
        fake_db, _ = _make_routing_db(configs_by_service={"radarr": radarr_cfg}, media_items=[])
        app.dependency_overrides[get_async_db] = fake_db

        async def hang_ids(_db):
            await asyncio.sleep(30)
            return set()

        with patch("media_manager.web.api.integrations.RadarrClient") as MockClient, \
             patch("media_manager.web.api.integrations.SEARCH_LIBRARY_IDS_TIMEOUT_SECONDS", 0.05), \
             patch("media_manager.web.api.integrations._get_radarr_tmdb_ids", new=hang_ids), \
             patch("media_manager.web.api.integrations._get_sonarr_tvdb_ids", new=hang_ids), \
             patch("media_manager.web.api.integrations._populate_media_details", new=AsyncMock()):
            instance = MockClient.return_value
            instance.search_movies = AsyncMock(return_value=[
                {"title": "Batman Begins", "tmdbId": 272, "overview": "desc", "year": 2005},
            ])
            response = self.client.get("/api/v1/integrations/search", params={"term": "batman"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Batman Begins", [r["title"] for r in response.json()["results"]])


class TestAddMediaToArr(_BaseIntegrationsTest):
    def test_add_movie_not_configured(self):
        fake_db, _ = _make_routing_db(configs_by_service={"radarr": None})
        app.dependency_overrides[get_async_db] = fake_db
        response = self.client.post("/api/v1/integrations/add", json={
            "type": "movie", "media_data": {"title": "X", "tmdbId": 1}
        })
        self.assertEqual(response.status_code, 400)

    def test_add_movie_success(self):
        cfg = _fake_config("radarr")
        fake_db, session = _make_routing_db(configs_by_service={"radarr": cfg}, metadata_cache=None)
        app.dependency_overrides[get_async_db] = fake_db
        with patch("media_manager.web.api.integrations.RadarrClient") as MockClient:
            instance = MockClient.return_value
            instance.get_profiles = AsyncMock(return_value=[{"id": 3}])
            instance.get_root_folders = AsyncMock(return_value=[{"path": "/movies"}])
            instance.add_movie = AsyncMock(return_value={"success": True})
            response = self.client.post("/api/v1/integrations/add", json={
                "type": "movie",
                "media_data": {"title": "New Movie", "tmdbId": 42},
            })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertTrue(session.committed)

    def test_add_movie_already_added_triggers_search(self):
        cfg = _fake_config("radarr")
        fake_db, session = _make_routing_db(configs_by_service={"radarr": cfg})
        app.dependency_overrides[get_async_db] = fake_db
        with patch("media_manager.web.api.integrations.RadarrClient") as MockClient:
            instance = MockClient.return_value
            instance.get_profiles = AsyncMock(return_value=[{"id": 3}])
            instance.get_root_folders = AsyncMock(return_value=[{"path": "/movies"}])
            instance.add_movie = AsyncMock(return_value={"success": False, "error": "already been added"})
            instance.get_movies = AsyncMock(return_value=[{"id": 99, "tmdbId": 42, "title": "New Movie"}])
            instance.trigger_command = AsyncMock(return_value={"success": True})
            response = self.client.post("/api/v1/integrations/add", json={
                "type": "movie",
                "media_data": {"title": "New Movie", "tmdbId": 42},
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn("already in Radarr", response.json()["message"])

    def test_add_movie_no_quality_profiles_raises(self):
        cfg = _fake_config("radarr")
        fake_db, _ = _make_routing_db(configs_by_service={"radarr": cfg})
        app.dependency_overrides[get_async_db] = fake_db
        with patch("media_manager.web.api.integrations.RadarrClient") as MockClient:
            instance = MockClient.return_value
            instance.get_profiles = AsyncMock(return_value=[])
            response = self.client.post("/api/v1/integrations/add", json={
                "type": "movie",
                "media_data": {"title": "New Movie", "tmdbId": 42},
            })
        self.assertEqual(response.status_code, 400)

    def test_add_series_success(self):
        cfg = _fake_config("sonarr")
        fake_db, session = _make_routing_db(configs_by_service={"sonarr": cfg})
        app.dependency_overrides[get_async_db] = fake_db
        with patch("media_manager.web.api.integrations.SonarrClient") as MockClient:
            instance = MockClient.return_value
            instance.get_profiles = AsyncMock(return_value=[{"id": 1}])
            instance.get_root_folders = AsyncMock(return_value=[{"path": "/tv"}])
            instance.add_series = AsyncMock(return_value={"success": True})
            response = self.client.post("/api/v1/integrations/add", json={
                "type": "tv",
                "media_data": {"title": "New Show", "tvdbId": 7},
            })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])

    def test_add_series_resolves_language_profile_from_sonarr(self):
        # Regression test: Sonarr v3 rejects series added without a valid
        # languageProfileId (it defaults to 0, which fails validation), so this
        # must be auto-resolved the same way quality profile / root folder are.
        cfg = _fake_config("sonarr")
        fake_db, session = _make_routing_db(configs_by_service={"sonarr": cfg})
        app.dependency_overrides[get_async_db] = fake_db
        with patch("media_manager.web.api.integrations.SonarrClient") as MockClient:
            instance = MockClient.return_value
            instance.get_profiles = AsyncMock(return_value=[{"id": 1}])
            instance.get_root_folders = AsyncMock(return_value=[{"path": "/tv"}])
            instance.get_language_profiles = AsyncMock(return_value=[{"id": 9, "name": "English"}])
            instance.add_series = AsyncMock(return_value={"success": True})
            response = self.client.post("/api/v1/integrations/add", json={
                "type": "tv",
                "media_data": {"title": "New Show", "tvdbId": 7},
            })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        _, kwargs = instance.add_series.call_args
        self.assertEqual(kwargs["language_profile_id"], 9)

    def test_add_series_omits_language_profile_when_sonarr_has_none(self):
        # Sonarr v4 dropped language profiles (no endpoint) - the add should
        # still succeed without a languageProfileId in that case.
        cfg = _fake_config("sonarr")
        fake_db, session = _make_routing_db(configs_by_service={"sonarr": cfg})
        app.dependency_overrides[get_async_db] = fake_db
        with patch("media_manager.web.api.integrations.SonarrClient") as MockClient:
            instance = MockClient.return_value
            instance.get_profiles = AsyncMock(return_value=[{"id": 1}])
            instance.get_root_folders = AsyncMock(return_value=[{"path": "/tv"}])
            instance.get_language_profiles = AsyncMock(return_value=[])
            instance.add_series = AsyncMock(return_value={"success": True})
            response = self.client.post("/api/v1/integrations/add", json={
                "type": "tv",
                "media_data": {"title": "New Show", "tvdbId": 7},
            })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        _, kwargs = instance.add_series.call_args
        self.assertIsNone(kwargs["language_profile_id"])

    def test_add_series_already_added_triggers_search(self):
        cfg = _fake_config("sonarr")
        fake_db, session = _make_routing_db(configs_by_service={"sonarr": cfg})
        app.dependency_overrides[get_async_db] = fake_db
        with patch("media_manager.web.api.integrations.SonarrClient") as MockClient:
            instance = MockClient.return_value
            instance.get_profiles = AsyncMock(return_value=[{"id": 1}])
            instance.get_root_folders = AsyncMock(return_value=[{"path": "/tv"}])
            instance.add_series = AsyncMock(return_value={"success": False, "error": "already been added"})
            instance.get_series = AsyncMock(return_value=[{"id": 5, "tvdbId": 7, "title": "New Show"}])
            instance.trigger_command = AsyncMock(return_value={"success": True})
            response = self.client.post("/api/v1/integrations/add", json={
                "type": "tv",
                "media_data": {"title": "New Show", "tvdbId": 7},
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn("already in Sonarr", response.json()["message"])


class TestSearchTrigger(_BaseIntegrationsTest):
    def test_movie_missing_tmdb_id_rejected(self):
        cfg = _fake_config("radarr")
        fake_db, _ = _make_routing_db(configs_by_service={"radarr": cfg})
        app.dependency_overrides[get_async_db] = fake_db
        response = self.client.post("/api/v1/integrations/search-trigger", json={"type": "movie"})
        self.assertEqual(response.status_code, 400)

    def test_movie_not_configured(self):
        fake_db, _ = _make_routing_db(configs_by_service={"radarr": None})
        app.dependency_overrides[get_async_db] = fake_db
        response = self.client.post("/api/v1/integrations/search-trigger", json={"type": "movie", "tmdb_id": 1})
        self.assertEqual(response.status_code, 400)

    def test_movie_not_found_in_radarr(self):
        cfg = _fake_config("radarr")
        fake_db, _ = _make_routing_db(configs_by_service={"radarr": cfg})
        app.dependency_overrides[get_async_db] = fake_db
        with patch("media_manager.web.api.integrations.RadarrClient") as MockClient:
            instance = MockClient.return_value
            instance.get_movies = AsyncMock(return_value=[])
            response = self.client.post("/api/v1/integrations/search-trigger", json={"type": "movie", "tmdb_id": 99})
        self.assertEqual(response.status_code, 404)

    def test_movie_search_triggered_successfully(self):
        cfg = _fake_config("radarr")
        fake_db, _ = _make_routing_db(configs_by_service={"radarr": cfg})
        app.dependency_overrides[get_async_db] = fake_db
        with patch("media_manager.web.api.integrations.RadarrClient") as MockClient:
            instance = MockClient.return_value
            instance.get_movies = AsyncMock(return_value=[{"id": 1, "tmdbId": 99, "title": "Movie"}])
            instance.trigger_command = AsyncMock(return_value={"success": True})
            response = self.client.post("/api/v1/integrations/search-trigger", json={"type": "movie", "tmdb_id": 99})
        self.assertTrue(response.json()["success"])

    def test_show_missing_tvdb_id_rejected(self):
        cfg = _fake_config("sonarr")
        fake_db, _ = _make_routing_db(configs_by_service={"sonarr": cfg})
        app.dependency_overrides[get_async_db] = fake_db
        response = self.client.post("/api/v1/integrations/search-trigger", json={"type": "tv"})
        self.assertEqual(response.status_code, 400)

    def test_show_not_found_in_sonarr(self):
        cfg = _fake_config("sonarr")
        fake_db, _ = _make_routing_db(configs_by_service={"sonarr": cfg})
        app.dependency_overrides[get_async_db] = fake_db
        with patch("media_manager.web.api.integrations.SonarrClient") as MockClient:
            instance = MockClient.return_value
            instance.get_series = AsyncMock(return_value=[])
            response = self.client.post("/api/v1/integrations/search-trigger", json={"type": "tv", "tvdb_id": 5})
        self.assertEqual(response.status_code, 404)

    def test_show_search_triggered_successfully(self):
        cfg = _fake_config("sonarr")
        fake_db, _ = _make_routing_db(configs_by_service={"sonarr": cfg})
        app.dependency_overrides[get_async_db] = fake_db
        with patch("media_manager.web.api.integrations.SonarrClient") as MockClient:
            instance = MockClient.return_value
            instance.get_series = AsyncMock(return_value=[{"id": 5, "tvdbId": 5, "title": "Show"}])
            instance.trigger_command = AsyncMock(return_value={"success": True})
            response = self.client.post("/api/v1/integrations/search-trigger", json={"type": "tv", "tvdb_id": 5})
        self.assertTrue(response.json()["success"])


if __name__ == "__main__":
    unittest.main()


class TestResolveAddDefaultsRootFolder(unittest.TestCase):
    """Regression: /add accepted any root_folder_path, even from non-admins."""

    def _client(self, folders):
        client = MagicMock()
        client.get_profiles = AsyncMock(return_value=[{"id": 7}])
        client.get_root_folders = AsyncMock(return_value=folders)
        return client

    def test_rejects_unknown_root_folder(self):
        from fastapi import HTTPException
        from media_manager.web.api.integrations import _resolve_add_defaults
        client = self._client([{"path": "/media/Movies/"}])
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(_resolve_add_defaults(client, "radarr", None, "/etc"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_accepts_known_root_folder_ignoring_trailing_slash(self):
        from media_manager.web.api.integrations import _resolve_add_defaults
        client = self._client([{"path": "/media/Movies/"}])
        profile, folder = asyncio.run(_resolve_add_defaults(client, "radarr", None, "/media/Movies"))
        self.assertEqual((profile, folder), (7, "/media/Movies"))

    def test_defaults_to_first_root_folder(self):
        from media_manager.web.api.integrations import _resolve_add_defaults
        client = self._client([{"path": "/tv"}, {"path": "/tv2"}])
        self.assertEqual(asyncio.run(_resolve_add_defaults(client, "sonarr", 3, None)), (3, "/tv"))
