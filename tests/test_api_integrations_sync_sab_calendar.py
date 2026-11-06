import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from media_manager.web.app import app
from media_manager.web.api.integrations import (
    _parse_arr_date,
    _normalize_arr_history,
)
from media_manager.web.api import integrations as integrations_module
from media_manager.web.api.auth import get_current_user, get_admin_user, get_current_active_user
from media_manager.database import get_async_db
from media_manager.models import IntegrationConfig, AppLogEvent


def _config(service_name, enabled=True, api_url="https://svc.invalid", api_key="key"):
    cfg = type("Cfg", (), {})()
    cfg.service_name = service_name
    cfg.api_url = api_url
    cfg.api_key_encrypted = api_key
    cfg.enabled = enabled
    return cfg


def _event(event_tag="[MEDIA_REQUEST]", message="", context_json=None):
    ev = type("Ev", (), {})()
    ev.event_tag = event_tag
    ev.message = message
    ev.context_json = context_json
    return ev


class _FakeResult:
    def __init__(self, scalar=None, scalars_list=None):
        self._scalar = scalar
        self._scalars_list = scalars_list if scalars_list is not None else []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        items = self._scalars_list

        class _S:
            def all(self_inner):
                return items

        return _S()


def _routing_db(configs=None, log_events=None):
    """Fake async DB session that answers integration_configs / app_log_events queries."""
    configs = configs or {}
    log_events = log_events or []

    class FakeSession:
        async def execute(self, statement, *args, **kwargs):
            compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
            if "integration_configs" in compiled:
                for name, cfg in configs.items():
                    if f"'{name}'" in compiled:
                        return _FakeResult(scalar=cfg)
                return _FakeResult(scalar=None)
            if "app_log_events" in compiled:
                return _FakeResult(scalars_list=log_events)
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


class BaseIntegrationsTest(unittest.TestCase):
    def setUp(self):
        admin_user = {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_admin_user] = lambda: admin_user
        app.dependency_overrides[get_current_active_user] = lambda: admin_user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()


class TestSyncthingHistory(BaseIntegrationsTest):
    def test_not_configured(self):
        app.dependency_overrides[get_async_db] = _routing_db()
        resp = self.client.get("/api/v1/integrations/syncthing/history")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["history"], [])

    def test_disabled_config(self):
        app.dependency_overrides[get_async_db] = _routing_db({"syncthing": _config("syncthing", enabled=False)})
        resp = self.client.get("/api/v1/integrations/syncthing/history")
        self.assertFalse(resp.json()["enabled"])

    def test_configured_filters_and_formats_events(self):
        app.dependency_overrides[get_async_db] = _routing_db({"syncthing": _config("syncthing")})
        now = datetime.utcnow()
        recent = (now - timedelta(days=1)).isoformat() + "Z"
        old = (now - timedelta(days=30)).isoformat() + "Z"
        events = [
            {"id": 1, "time": recent, "data": {"folder": "f1", "item": "/x/movie.mkv", "action": "update"}},
            {"id": 2, "time": recent, "data": {"folder": "f1", "item": "/x/deleted.mkv", "action": "delete"}},
            {"id": 3, "time": old, "data": {"folder": "f1", "item": "/x/too_old.mkv"}},
            {"id": 4, "time": "not-a-date", "data": {}},
            {"id": 5, "time": recent, "data": {"folder": "f2", "item": "/x/err.mkv", "error": "boom"}},
        ]
        with patch.object(integrations_module, "SyncthingClient") as MockClient:
            instance = MockClient.return_value
            instance.get_events = AsyncMock(return_value=events)
            resp = self.client.get("/api/v1/integrations/syncthing/history?days=7")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["enabled"])
        names = [h["filename"] for h in data["history"]]
        self.assertIn("movie.mkv", names)
        self.assertIn("err.mkv", names)
        self.assertNotIn("deleted.mkv", names)
        self.assertNotIn("too_old.mkv", names)
        err_entry = next(h for h in data["history"] if h["filename"] == "err.mkv")
        self.assertEqual(err_entry["status"], "Failed")


class TestSabnzbdQueue(BaseIntegrationsTest):
    def test_not_configured(self):
        app.dependency_overrides[get_async_db] = _routing_db()
        resp = self.client.get("/api/v1/integrations/sabnzbd/queue")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["queue"], [])
        self.assertIn("warning", data)

    def test_configured_returns_slots(self):
        app.dependency_overrides[get_async_db] = _routing_db({"sabnzbd": _config("sabnzbd")})
        queue = {
            "speed": "5 MB/s",
            "sizeleft": "2 GB",
            "slots": [{"nzo_id": "1", "filename": "a.mkv", "size": "1 GB", "percentage": "50", "timeleft": "0:10:00"}],
        }
        with patch.object(integrations_module, "SABnzbdClient") as MockClient:
            instance = MockClient.return_value
            instance.get_queue = AsyncMock(return_value=queue)
            resp = self.client.get("/api/v1/integrations/sabnzbd/queue")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["speed"], "5 MB/s")
        self.assertEqual(len(data["queue"]), 1)
        self.assertEqual(data["queue"][0]["filename"], "a.mkv")


class TestParseArrDate(unittest.TestCase):
    def test_empty_returns_zero(self):
        self.assertEqual(_parse_arr_date(None), 0.0)
        self.assertEqual(_parse_arr_date(""), 0.0)

    def test_standard_iso_z(self):
        self.assertGreater(_parse_arr_date("2024-01-01T00:00:00Z"), 0.0)

    def test_nonstandard_fraction_width(self):
        # 5-digit fractional seconds - not directly accepted by fromisoformat
        self.assertGreater(_parse_arr_date("2024-01-01T00:00:00.99791Z"), 0.0)

    def test_malformed_returns_zero(self):
        self.assertEqual(_parse_arr_date("not-a-date-at-all"), 0.0)


class TestNormalizeArrHistory(unittest.TestCase):
    def test_only_import_events_included(self):
        records = [
            {"eventType": "grabbed", "downloadId": "d1", "data": {"size": 1000}},
            {"eventType": "grabbed", "downloadId": "d2", "data": {}},
            {
                "eventType": "downloadFolderImported",
                "downloadId": "d1",
                "sourceTitle": "Movie.1",
                "data": {},
                "date": "2024-01-01T00:00:00Z",
                "id": 5,
            },
            {"eventType": "seriesAdd", "downloadId": "d3"},
        ]
        result = _normalize_arr_history(records, "movies")
        self.assertEqual(len(result), 1)
        entry = result[0]
        self.assertEqual(entry["nzo_id"], "d1")
        self.assertEqual(entry["name"], "Movie.1")
        self.assertEqual(entry["category"], "movies")
        # size fell back to the paired grabbed event's size (1000 bytes)
        self.assertIn("GB", entry["size"])

    def test_missing_download_id_falls_back_to_synthetic_id(self):
        records = [
            {
                "eventType": "downloadFolderImported",
                "downloadId": None,
                "sourceTitle": "Show S01E01",
                "data": {"size": 500_000_000},
                "date": "2024-01-01T00:00:00Z",
                "id": 42,
            }
        ]
        result = _normalize_arr_history(records, "tv")
        self.assertEqual(result[0]["nzo_id"], "tv-history-42")
        self.assertEqual(result[0]["bytes"], 500_000_000)

    def test_no_size_anywhere_yields_empty_size_string(self):
        records = [
            {
                "eventType": "downloadFolderImported",
                "downloadId": "d9",
                "sourceTitle": "X",
                "data": {},
                "date": None,
                "id": 1,
            }
        ]
        result = _normalize_arr_history(records, "movies")
        self.assertEqual(result[0]["size"], "")
        self.assertEqual(result[0]["completed"], 0.0)


class TestSabnzbdHistory(BaseIntegrationsTest):
    def test_nothing_configured_returns_empty_stats(self):
        app.dependency_overrides[get_async_db] = _routing_db()
        resp = self.client.get("/api/v1/integrations/sabnzbd/history")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["slots"], [])
        self.assertEqual(data["stats"]["7d"]["count"], 0)

    def test_merges_sab_sonarr_radarr_dedupes_by_nzo_id(self):
        configs = {
            "sabnzbd": _config("sabnzbd"),
            "sonarr": _config("sonarr"),
            "radarr": _config("radarr"),
        }
        app.dependency_overrides[get_async_db] = _routing_db(configs)
        now_ts = datetime.utcnow().timestamp()

        sab_history = {"slots": [{"nzo_id": "shared-1", "name": "SabName", "size": "1 GB",
                                   "status": "Completed", "category": "movies",
                                   "completed": now_ts, "bytes": 1_000_000_000}]}
        sonarr_history = [{
            "eventType": "downloadFolderImported", "downloadId": "shared-1",
            "sourceTitle": "DupedFromSonarr", "data": {"size": 200}, "date": "2024-01-01T00:00:00Z", "id": 1,
        }, {
            "eventType": "downloadFolderImported", "downloadId": "sonarr-only",
            "sourceTitle": "SonarrOnly", "data": {"size": 300}, "date": "2024-01-01T00:00:00Z", "id": 2,
        }]
        radarr_history = [{
            "eventType": "downloadFolderImported", "downloadId": "radarr-only",
            "sourceTitle": "RadarrOnly", "data": {"size": 400}, "date": "2024-01-01T00:00:00Z", "id": 3,
        }]

        with patch.object(integrations_module, "SABnzbdClient") as MockSab, \
             patch.object(integrations_module, "SonarrClient") as MockSonarr, \
             patch.object(integrations_module, "RadarrClient") as MockRadarr:
            MockSab.return_value.get_history = AsyncMock(return_value=sab_history)
            MockSonarr.return_value.get_history = AsyncMock(return_value=sonarr_history)
            MockRadarr.return_value.get_history = AsyncMock(return_value=radarr_history)
            resp = self.client.get("/api/v1/integrations/sabnzbd/history")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        nzo_ids = {s["nzo_id"] for s in data["slots"]}
        # shared-1 came from SAB (richer source), not duplicated by sonarr's version
        self.assertIn("shared-1", nzo_ids)
        self.assertIn("sonarr-only", nzo_ids)
        self.assertIn("radarr-only", nzo_ids)
        self.assertEqual(len(data["slots"]), 3)
        self.assertGreaterEqual(data["stats"]["30d"]["count"], 1)

    def test_config_lookups_do_not_share_session_concurrently(self):
        """Regression test: the sabnzbd/sonarr/radarr config lookups used to run
        under asyncio.gather() on one shared AsyncSession, which real SQLAlchemy
        forbids (raises IllegalStateChangeError / "concurrent operations are not
        permitted") and leaked the underlying connection. This fake session raises
        the same way if it's ever re-entered before a prior execute() completes."""
        import asyncio

        configs = {
            "sabnzbd": _config("sabnzbd"),
            "sonarr": _config("sonarr"),
            "radarr": _config("radarr"),
        }

        class _ConcurrencyGuardedSession:
            def __init__(self):
                self._busy = False

            async def execute(self, statement, *args, **kwargs):
                if self._busy:
                    raise RuntimeError(
                        "concurrent operations are not permitted on this session"
                    )
                self._busy = True
                try:
                    await asyncio.sleep(0)
                    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
                    for name, cfg in configs.items():
                        if f"'{name}'" in compiled:
                            return _FakeResult(scalar=cfg)
                    return _FakeResult(scalar=None)
                finally:
                    self._busy = False

            def add(self, *a, **k):
                pass

            async def commit(self):
                pass

            async def rollback(self):
                pass

        async def fake_get_async_db():
            yield _ConcurrencyGuardedSession()

        app.dependency_overrides[get_async_db] = fake_get_async_db

        with patch.object(integrations_module, "SABnzbdClient") as MockSab, \
             patch.object(integrations_module, "SonarrClient") as MockSonarr, \
             patch.object(integrations_module, "RadarrClient") as MockRadarr:
            MockSab.return_value.get_history = AsyncMock(return_value={"slots": []})
            MockSonarr.return_value.get_history = AsyncMock(return_value=[])
            MockRadarr.return_value.get_history = AsyncMock(return_value=[])
            resp = self.client.get("/api/v1/integrations/sabnzbd/history")

        self.assertEqual(resp.status_code, 200)


class TestRequestUserMappings(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # _get_request_user_mappings is TTL-cached at module scope; clear it so each
        # test's fake DB contents are actually re-read instead of a prior test's result.
        integrations_module.IN_MEMORY_REQUEST_USER_MAPPINGS_CACHE.clear()

    async def test_context_json_path(self):
        ev = _event(context_json='{"username": "alice", "tmdb_id": 5, "tvdb_id": 9, "title": "Some Movie"}')
        db = _FakeDbForMappings([ev])
        tmdb_map, tvdb_map, title_map = await integrations_module._get_request_user_mappings(db)
        self.assertEqual(tmdb_map[5], "alice")
        self.assertEqual(tvdb_map[9], "alice")
        self.assertEqual(title_map["some movie"], "alice")

    async def test_regex_fallback_path(self):
        ev = _event(message="User 'bob' requested movie 'Cool Film' (TMDb: 42)")
        db = _FakeDbForMappings([ev])
        tmdb_map, tvdb_map, title_map = await integrations_module._get_request_user_mappings(db)
        self.assertEqual(tmdb_map[42], "bob")
        self.assertEqual(title_map["cool film"], "bob")

    async def test_malformed_context_json_ignored(self):
        ev = _event(context_json="{not valid json")
        db = _FakeDbForMappings([ev])
        tmdb_map, tvdb_map, title_map = await integrations_module._get_request_user_mappings(db)
        self.assertEqual(tmdb_map, {})
        self.assertEqual(title_map, {})


class _FakeDbForMappings:
    def __init__(self, events):
        self._events = events

    async def execute(self, *a, **k):
        return _FakeResult(scalars_list=self._events)


class TestCombinedCalendar(BaseIntegrationsTest):
    def test_no_services_configured_returns_empty_list(self):
        app.dependency_overrides[get_async_db] = _routing_db()
        with patch.object(integrations_module, "_populate_media_details", new=AsyncMock()):
            resp = self.client.get("/api/v1/integrations/calendar")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_radarr_and_sonarr_events_merged_and_sorted(self):
        configs = {"radarr": _config("radarr"), "sonarr": _config("sonarr")}
        app.dependency_overrides[get_async_db] = _routing_db(configs)
        movies = [{
            "title": "A Movie", "year": 2024, "physicalRelease": "2024-06-15T00:00:00Z",
            "hasFile": True, "monitored": True, "overview": "ov", "tmdbId": 1,
        }]
        series = [{"id": 1, "title": "A Show", "tvdbId": 99}]
        episodes = [{
            "airDate": "2024-06-01", "seriesId": 1, "seasonNumber": 1, "episodeNumber": 2,
            "title": "Ep Title", "hasFile": False, "monitored": True, "overview": "ov2",
        }]
        with patch.object(integrations_module, "RadarrClient") as MockRadarr, \
             patch.object(integrations_module, "SonarrClient") as MockSonarr, \
             patch.object(integrations_module, "_populate_media_details", new=AsyncMock()):
            MockRadarr.return_value.get_calendar = AsyncMock(return_value=movies)
            MockSonarr.return_value.get_series = AsyncMock(return_value=series)
            MockSonarr.return_value.get_calendar = AsyncMock(return_value=episodes)
            resp = self.client.get("/api/v1/integrations/calendar")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 2)
        types = {d["type"] for d in data}
        self.assertEqual(types, {"movie", "tv"})
        # sorted by date ascending
        self.assertLessEqual(data[0]["date"], data[1]["date"])

    def test_explicit_date_range_overrides_default_window(self):
        configs = {"radarr": _config("radarr"), "sonarr": _config("sonarr")}
        app.dependency_overrides[get_async_db] = _routing_db(configs)
        with patch.object(integrations_module, "RadarrClient") as MockRadarr, \
             patch.object(integrations_module, "SonarrClient") as MockSonarr, \
             patch.object(integrations_module, "_populate_media_details", new=AsyncMock()):
            MockRadarr.return_value.get_calendar = AsyncMock(return_value=[])
            MockSonarr.return_value.get_series = AsyncMock(return_value=[])
            MockSonarr.return_value.get_calendar = AsyncMock(return_value=[])
            resp = self.client.get("/api/v1/integrations/calendar?start_date=2027-03-01&end_date=2027-03-31")
        self.assertEqual(resp.status_code, 200)
        MockRadarr.return_value.get_calendar.assert_awaited_once_with("2027-03-01", "2027-03-31")
        MockSonarr.return_value.get_calendar.assert_awaited_once_with("2027-03-01", "2027-03-31")

    def test_client_errors_are_logged_and_swallowed(self):
        configs = {"radarr": _config("radarr"), "sonarr": _config("sonarr")}
        app.dependency_overrides[get_async_db] = _routing_db(configs)
        with patch.object(integrations_module, "RadarrClient") as MockRadarr, \
             patch.object(integrations_module, "SonarrClient") as MockSonarr, \
             patch.object(integrations_module, "_populate_media_details", new=AsyncMock()):
            MockRadarr.return_value.get_calendar = AsyncMock(side_effect=Exception("radarr down"))
            MockSonarr.return_value.get_series = AsyncMock(side_effect=Exception("sonarr down"))
            resp = self.client.get("/api/v1/integrations/calendar")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])


class TestDownloadQueue(BaseIntegrationsTest):
    def test_no_services_configured_returns_empty_queue(self):
        app.dependency_overrides[get_async_db] = _routing_db()
        resp = self.client.get("/api/v1/integrations/download-queue")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"count": 0, "queue": []})

    def test_radarr_and_sonarr_queues_merged_and_normalized(self):
        configs = {"radarr": _config("radarr"), "sonarr": _config("sonarr")}
        app.dependency_overrides[get_async_db] = _routing_db(configs)
        radarr_records = [{
            "id": 1, "movie": {"title": "A Movie"}, "size": 1000, "sizeleft": 250,
            "timeleft": "00:10:00", "status": "downloading", "trackedDownloadStatus": "ok",
            "quality": {"quality": {"name": "Bluray-2160p"}}, "indexer": "IndexerA",
            "downloadClient": "SABnzbd", "protocol": "usenet", "errorMessage": None
        }]
        sonarr_records = [{
            "id": 2, "series": {"title": "A Show"}, "episode": {"seasonNumber": 2, "episodeNumber": 5},
            "size": 500, "sizeleft": 0, "timeleft": None, "status": "completed",
            "trackedDownloadStatus": "ok", "quality": {"quality": {"name": "WEBDL-1080p"}},
            "indexer": "IndexerB", "downloadClient": "SABnzbd", "protocol": "usenet", "errorMessage": None
        }]
        with patch.object(integrations_module, "RadarrClient") as MockRadarr, \
             patch.object(integrations_module, "SonarrClient") as MockSonarr:
            MockRadarr.return_value.get_queue = AsyncMock(return_value=radarr_records)
            MockSonarr.return_value.get_queue = AsyncMock(return_value=sonarr_records)
            resp = self.client.get("/api/v1/integrations/download-queue")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 2)
        by_type = {q["type"]: q for q in data["queue"]}
        self.assertEqual(by_type["movie"]["title"], "A Movie")
        self.assertEqual(by_type["movie"]["progress_pct"], 75.0)
        self.assertEqual(by_type["movie"]["quality"], "Bluray-2160p")
        self.assertEqual(by_type["tv"]["title"], "A Show - S02E05")
        self.assertEqual(by_type["tv"]["progress_pct"], 100.0)

    def test_client_errors_are_logged_and_swallowed(self):
        configs = {"radarr": _config("radarr"), "sonarr": _config("sonarr")}
        app.dependency_overrides[get_async_db] = _routing_db(configs)
        with patch.object(integrations_module, "RadarrClient") as MockRadarr, \
             patch.object(integrations_module, "SonarrClient") as MockSonarr:
            MockRadarr.return_value.get_queue = AsyncMock(side_effect=Exception("radarr down"))
            MockSonarr.return_value.get_queue = AsyncMock(side_effect=Exception("sonarr down"))
            resp = self.client.get("/api/v1/integrations/download-queue")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"count": 0, "queue": []})

    def test_queue_action_remove_calls_radarr(self):
        configs = {"radarr": _config("radarr")}
        app.dependency_overrides[get_async_db] = _routing_db(configs)
        with patch.object(integrations_module, "RadarrClient") as MockRadarr:
            MockRadarr.return_value.delete_queue_item = AsyncMock(return_value=True)
            resp = self.client.post(
                "/api/v1/integrations/download-queue/action",
                json={"action": "remove", "type": "movie", "id": 9},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        MockRadarr.return_value.delete_queue_item.assert_awaited_once_with(9)

    def test_queue_action_search_requires_ids(self):
        configs = {"radarr": _config("radarr")}
        app.dependency_overrides[get_async_db] = _routing_db(configs)
        resp = self.client.post(
            "/api/v1/integrations/download-queue/action",
            json={"action": "search", "type": "movie", "id": 1},
        )
        self.assertEqual(resp.status_code, 400)

    def test_add_options_returns_profiles(self):
        configs = {"radarr": _config("radarr")}
        app.dependency_overrides[get_async_db] = _routing_db(configs)
        with patch.object(integrations_module, "RadarrClient") as MockRadarr:
            MockRadarr.return_value.get_profiles = AsyncMock(return_value=[{"id": 4, "name": "HD"}])
            resp = self.client.get("/api/v1/integrations/add-options?type=movie")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["default_profile_id"], 4)
        self.assertEqual(resp.json()["profiles"][0]["name"], "HD")


class TestImgProxy(BaseIntegrationsTest):
    def setUp(self):
        super().setUp()
        integrations_module.IN_MEMORY_THUMB_CACHE.clear()

    def test_missing_url_returns_400(self):
        resp = self.client.get("/api/v1/integrations/imgproxy?url=")
        self.assertEqual(resp.status_code, 400)

    def test_memory_cache_hit(self):
        url = "https://image.tmdb.org/t/p/w185/x.jpg"
        key = integrations_module.hashlib.md5(f"{url}:350:500".encode("utf-8")).hexdigest()
        integrations_module.IN_MEMORY_THUMB_CACHE[key] = b"cached-bytes"
        resp = self.client.get(f"/api/v1/integrations/imgproxy?url={url}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"cached-bytes")

    def test_disallowed_remote_host_returns_400(self):
        resp = self.client.get("/api/v1/integrations/imgproxy?url=http://127.0.0.1/secret.png")
        self.assertEqual(resp.status_code, 400)

    def test_remote_fetch_and_resize_success(self):
        import io as _io
        from PIL import Image as _Image

        buf = _io.BytesIO()
        _Image.new("RGB", (10, 10), color="red").save(buf, format="PNG")
        fake_image_bytes = buf.getvalue()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = fake_image_bytes

        mock_async_client = AsyncMock()
        mock_async_client.get = AsyncMock(return_value=mock_resp)
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(integrations_module.httpx, "AsyncClient", return_value=mock_async_client), \
             patch.object(integrations_module.os.path, "exists", return_value=False):
            resp = self.client.get("/api/v1/integrations/imgproxy?url=https://image.tmdb.org/t/p/original/pic.png")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/jpeg")

    def test_remote_fetch_exception_then_404(self):
        mock_async_client = AsyncMock()
        mock_async_client.get = AsyncMock(side_effect=Exception("network down"))
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        with patch.object(integrations_module.httpx, "AsyncClient", return_value=mock_async_client):
            resp = self.client.get("/api/v1/integrations/imgproxy?url=https://image.tmdb.org/t/p/original/missing.png")
        self.assertEqual(resp.status_code, 404)

    def test_local_file_not_found_returns_404(self):
        with patch.object(integrations_module.os.path, "exists", return_value=False):
            resp = self.client.get("/api/v1/integrations/imgproxy?url=/no/such/file.jpg")
        self.assertEqual(resp.status_code, 404)

    def test_local_file_success_and_disk_cache_reuse(self):
        import io as _io
        from PIL import Image as _Image
        import tempfile, os as _os

        buf = _io.BytesIO()
        _Image.new("RGB", (5, 5), color="blue").save(buf, format="PNG")
        with tempfile.TemporaryDirectory() as tmpdir:
            src_path = _os.path.join(tmpdir, "local.png")
            with open(src_path, "wb") as f:
                f.write(buf.getvalue())
            with patch.object(integrations_module, "CACHE_DIR", tmpdir), \
                 patch("media_manager.config.MEDIA_DIRECTORIES", [tmpdir]), \
                 patch("media_manager.config.SOURCE_DIRECTORIES", []):
                resp = self.client.get(f"/api/v1/integrations/imgproxy?url={src_path}")
                self.assertEqual(resp.status_code, 200)
                # second call should hit the disk-cache branch (memory cache cleared first)
                integrations_module.IN_MEMORY_THUMB_CACHE.clear()
                with patch.object(integrations_module, "CACHE_DIR", tmpdir), \
                     patch("media_manager.config.MEDIA_DIRECTORIES", [tmpdir]), \
                     patch("media_manager.config.SOURCE_DIRECTORIES", []):
                    resp2 = self.client.get(f"/api/v1/integrations/imgproxy?url={src_path}")
                self.assertEqual(resp2.status_code, 200)

    def test_serve_local_art_redirect_path_prefix(self):
        with patch.object(integrations_module.os.path, "exists", return_value=False):
            resp = self.client.get(
                "/api/v1/integrations/imgproxy?url=/api/v1/integrations/serve-local-art?path=%2Ftmp%2Ffoo.jpg"
            )
        self.assertEqual(resp.status_code, 404)

    def test_processing_failure_returns_500(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"not a real image"
        mock_async_client = AsyncMock()
        mock_async_client.get = AsyncMock(return_value=mock_resp)
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        with patch.object(integrations_module.httpx, "AsyncClient", return_value=mock_async_client):
            resp = self.client.get("/api/v1/integrations/imgproxy?url=https://image.tmdb.org/t/p/original/bad.png")
        self.assertEqual(resp.status_code, 500)


class TestServeLocalArt(BaseIntegrationsTest):
    def test_missing_path_404(self):
        with patch.object(integrations_module.os.path, "exists", return_value=False):
            resp = self.client.get("/api/v1/integrations/serve-local-art?path=/no/such.jpg")
        self.assertEqual(resp.status_code, 404)

    def test_invalid_extension_400(self):
        import tempfile, os as _os
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"not an image")
            path = f.name
        try:
            resp = self.client.get(f"/api/v1/integrations/serve-local-art?path={path}")
            self.assertEqual(resp.status_code, 400)
        finally:
            _os.remove(path)

    def test_valid_image_served(self):
        import tempfile, os as _os
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"fake-jpeg-bytes")
            path = f.name
        try:
            with patch("media_manager.config.MEDIA_DIRECTORIES", [_os.path.dirname(path)]), \
                 patch("media_manager.config.SOURCE_DIRECTORIES", []):
                resp = self.client.get(f"/api/v1/integrations/serve-local-art?path={path}")
            self.assertEqual(resp.status_code, 200)
        finally:
            _os.remove(path)


if __name__ == "__main__":
    unittest.main()
