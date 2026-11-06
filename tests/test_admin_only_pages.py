"""Cleanup, empty-file re-download and the H.265 migration are admin-only."""
import os
import re
import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from media_manager.web.app import app
from media_manager.web.api.auth import get_current_active_user

STATIC = os.path.join(os.path.dirname(__file__), "..", "media_manager", "web", "static")

ADMIN_ONLY_ENDPOINTS = [
    ("GET", "/api/v1/cleanup/settings"),
    ("GET", "/api/v1/cleanup/suggestions"),
    ("GET", "/api/v1/redownload/empty-files"),
    ("GET", "/api/v1/transcode/probe-backfill"),
    ("GET", "/api/v1/transcode/settings"),
    ("GET", "/api/v1/transcode/control"),
    ("GET", "/api/v1/transcode/dry-run"),
    ("GET", "/api/v1/transcode/candidates"),
    ("GET", "/api/v1/transcode/status"),
    ("GET", "/api/v1/transcode/jobs"),
    ("GET", "/api/v1/transcode/jobs/some-id"),
]


class TestAdminOnlyApis(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_regular_users_get_403_through_the_real_admin_check(self):
        app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(role="user", is_active=True)
        for method, path in ADMIN_ONLY_ENDPOINTS:
            with self.subTest(path=path):
                self.assertEqual(self.client.request(method, path).status_code, 403)

    def test_anonymous_users_get_401(self):
        for method, path in ADMIN_ONLY_ENDPOINTS:
            with self.subTest(path=path):
                self.assertEqual(self.client.request(method, path).status_code, 401)


class TestAdminOnlyNavigation(unittest.TestCase):
    def _read(self, *parts):
        with open(os.path.join(STATIC, *parts)) as fh:
            return fh.read()

    def test_nav_items_are_marked_admin_only(self):
        html = self._read("index.html")
        for nav_id in ("nav-cleanup", "nav-transcode", "nav-admin", "nav-admin-setup", "nav-paths", "nav-users", "nav-playback", "nav-downloads", "nav-duplicates", "nav-logs"):
            with self.subTest(nav=nav_id):
                tag = re.search(rf'<a [^>]*id="{nav_id}"[^>]*>', html).group(0)
                self.assertIn("admin-only", tag)

    def test_router_and_auth_guard_the_views(self):
        auth_js = self._read("js", "auth.js")
        views = re.search(r"ADMIN_ONLY_VIEWS = \[([^\]]*)\]", auth_js).group(1)
        for view in ("admin", "paths", "users", "playback", "cleanup", "transcode", "downloads", "duplicates", "logs"):
            self.assertIn(f"'{view}'", views)
        self.assertIn("querySelectorAll('.admin-only')", auth_js)
        self.assertIn("ADMIN_ONLY_VIEWS || []).includes(viewId)", self._read("index.html"))


if __name__ == "__main__":
    unittest.main()
