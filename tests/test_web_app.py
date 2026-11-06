import unittest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from media_manager.web.app import app, STATIC_DIR


class TestWebAppEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_get_version(self):
        response = self.client.get("/api/v1/version")
        self.assertEqual(response.status_code, 200)
        self.assertIn("version", response.json())

    def test_serve_index_fallback_message_when_no_static_build(self):
        with patch("os.path.exists", return_value=False):
            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("message", response.json())

    def test_serve_index_returns_file_when_present(self):
        with patch("os.path.exists", return_value=True), \
             patch("media_manager.web.app.FileResponse") as mock_file_response:
            mock_file_response.return_value = "fake-file-response"
            # Bypass the TestClient's response validation by calling the
            # route function directly - simplest way to prove the "file
            # exists" branch calls FileResponse with the right path.
            import asyncio
            from media_manager.web.app import serve_index
            asyncio.run(serve_index())
            mock_file_response.assert_called_once_with(
                __import__("os").path.join(STATIC_DIR, "index.html")
            )

    def test_head_index(self):
        response = self.client.head("/")
        self.assertEqual(response.status_code, 200)

    def test_global_exception_handler(self):
        # Add a throwaway route that always raises, to exercise the
        # app-wide exception handler registered in app.py. TestClient
        # re-raises server exceptions by default (raise_server_exceptions
        # defaults to True) which would bypass the handler under test, so a
        # dedicated client disables that here.
        @app.get("/__boom__")
        async def boom():
            raise ValueError("kaboom")

        non_raising_client = TestClient(app, raise_server_exceptions=False)
        response = non_raising_client.get("/__boom__")
        self.assertEqual(response.status_code, 500)
        data = response.json()
        self.assertEqual(data["detail"], "Internal server error occurred.")
        self.assertNotIn("error", data)

        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", None) != "/__boom__"]


class TestWebAppLifespan(unittest.TestCase):
    def test_lifespan_starts_and_cancels_sync_task(self):
        # Exercises app.py's lifespan context manager: schema init + starting
        # the background metadata-sync scheduler task on startup, and
        # cancelling it on shutdown. weekly_metadata_sync_scheduler itself is
        # mocked out so nothing sleeps for a real hour.
        with patch("media_manager.web.app.init_db_schema", new=AsyncMock()) as mock_init, \
             patch("media_manager.web.services.metadata_sync.weekly_metadata_sync_scheduler", new=AsyncMock()):
            with TestClient(app) as client:
                response = client.get("/api/v1/version")
                self.assertEqual(response.status_code, 200)
            mock_init.assert_called_once()

    def test_lifespan_tolerates_schema_init_failure(self):
        with patch("media_manager.web.app.init_db_schema", new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("media_manager.web.services.metadata_sync.weekly_metadata_sync_scheduler", new=AsyncMock()):
            # Startup must not raise even if schema init fails.
            with TestClient(app) as client:
                response = client.get("/api/v1/version")
                self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
