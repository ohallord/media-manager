import unittest

from fastapi.testclient import TestClient

from media_manager.web.app import app


class TestIntegrationsRequireAuth(unittest.TestCase):
    """Previously public GETs leaked library membership, NZB names, and local art."""

    def setUp(self):
        app.dependency_overrides.clear()
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_unauthenticated_discovery_and_download_routes_return_401(self):
        paths = [
            "/api/v1/integrations/upcoming-movies",
            "/api/v1/integrations/upcoming-tv",
            "/api/v1/integrations/trending",
            "/api/v1/integrations/watcher-status",
            "/api/v1/integrations/add-options?type=movie",
            "/api/v1/integrations/artwork?title=x",
            "/api/v1/integrations/metadata?title=x",
            "/api/v1/integrations/episode-metadata?show_title=x&season=1&episode=1",
            "/api/v1/integrations/sabnzbd/queue",
            "/api/v1/integrations/sabnzbd/history",
            "/api/v1/integrations/syncthing/downloads",
            "/api/v1/integrations/syncthing/history",
            "/api/v1/integrations/imgproxy?url=http://example.invalid/x.jpg",
            "/api/v1/integrations/serve-local-art?path=/tmp/x.jpg",
        ]
        for path in paths:
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 401, msg=path)
