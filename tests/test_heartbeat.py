import unittest
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from media_manager.database import SyncSessionLocal
from media_manager.heartbeat import tick_watcher_heartbeat, WATCHER_SERVICE, STALE_AFTER_SECONDS
from media_manager.models import ServiceHeartbeat
from media_manager.web.app import app


class TestWatcherHeartbeat(unittest.TestCase):
    def setUp(self):
        from media_manager.web.api.auth import get_current_user, get_admin_user
        app.dependency_overrides[get_current_user] = lambda: {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_admin_user] = lambda: {"username": "admin", "role": "admin"}
        self.client = TestClient(app)
        session = SyncSessionLocal()
        try:
            session.query(ServiceHeartbeat).filter_by(service=WATCHER_SERVICE).delete()
            session.commit()
        finally:
            session.close()

    def tearDown(self):
        app.dependency_overrides.clear()
        session = SyncSessionLocal()
        try:
            session.query(ServiceHeartbeat).filter_by(service=WATCHER_SERVICE).delete()
            session.commit()
        finally:
            session.close()

    def test_tick_writes_row(self):
        tick_watcher_heartbeat()
        session = SyncSessionLocal()
        try:
            row = session.query(ServiceHeartbeat).filter_by(service=WATCHER_SERVICE).first()
            self.assertIsNotNone(row)
            self.assertIsNotNone(row.last_beat_at)
        finally:
            session.close()

    def test_status_alive_after_tick(self):
        tick_watcher_heartbeat()
        resp = self.client.get("/api/v1/integrations/watcher-status")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["alive"])
        self.assertFalse(resp.json()["stale"])

    def test_status_stale_when_old(self):
        tick_watcher_heartbeat()
        session = SyncSessionLocal()
        try:
            row = session.query(ServiceHeartbeat).filter_by(service=WATCHER_SERVICE).first()
            row.last_beat_at = datetime.utcnow() - timedelta(seconds=STALE_AFTER_SECONDS + 20)
            session.commit()
        finally:
            session.close()
        resp = self.client.get("/api/v1/integrations/watcher-status")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["alive"])
        self.assertTrue(resp.json()["stale"])

    def test_http_ingest_without_token_is_401(self):
        resp = self.client.post("/api/v1/watcher/heartbeat", json={"pid": 1})
        self.assertEqual(resp.status_code, 401)

    def test_http_ingest_with_token_marks_alive(self):
        from media_manager.heartbeat import DEFAULT_TOKEN
        resp = self.client.post(
            "/api/v1/watcher/heartbeat",
            json={"pid": 42, "hostname": "testhost"},
            headers={"X-Watcher-Token": DEFAULT_TOKEN},
        )
        self.assertEqual(resp.status_code, 200)
        status = self.client.get("/api/v1/integrations/watcher-status")
        self.assertTrue(status.json()["alive"])
        self.assertEqual(status.json()["pid"], 42)

    def test_tick_http_tries_comma_separated_urls(self):
        import os
        from unittest.mock import patch
        from media_manager.heartbeat import tick_http

        os.environ["WATCHER_HEARTBEAT_HTTP"] = "1"
        os.environ["WATCHER_HEARTBEAT_URL"] = "http://127.0.0.1:1/bad,http://127.0.0.1:2/ok"
        calls = []

        def fake_urlopen(req, timeout=4, context=None):
            calls.append(req.full_url)
            if req.full_url.endswith("/bad"):
                raise OSError("refused")
            return None

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            tick_http()
        self.assertEqual(calls, ["http://127.0.0.1:1/bad", "http://127.0.0.1:2/ok"])
        os.environ["WATCHER_HEARTBEAT_HTTP"] = "0"
