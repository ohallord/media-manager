import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from media_manager.web.app import app
from media_manager.web.api import redownload as redownload_module
from media_manager.web.api.auth import get_admin_user, get_current_user

ITEMS = [{"media_item_id": "1", "status": "radarr_search", "path": "/m/a.mkv", "detail": "A (2000)"}]


class TestRedownloadEndpoints(unittest.TestCase):
    def setUp(self):
        self.admin_user = {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        app.dependency_overrides[get_admin_user] = lambda: self.admin_user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_preview_lists_items_without_internal_state(self):
        plan = {"items": ITEMS, "state": {"radarr": object()}, "episodes_cache": {}}
        with patch.object(redownload_module, "plan_redownload", AsyncMock(return_value=plan)):
            res = self.client.get("/api/v1/redownload/empty-files")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"count": 1, "items": ITEMS})

    def test_post_all_and_by_ids(self):
        run = AsyncMock(return_value={"counts": {"requested": 1}, "results": ITEMS})
        with patch.object(redownload_module, "run_redownload", run):
            res = self.client.post("/api/v1/redownload/empty-files", json={"all": True})
            self.assertEqual(res.status_code, 200)
            self.assertIsNone(run.await_args.args[1])
            self.client.post("/api/v1/redownload/empty-files", json={"media_item_ids": ["1"]})
            self.assertEqual(run.await_args.args[1], ["1"])

    def test_post_requires_a_selection(self):
        res = self.client.post("/api/v1/redownload/empty-files", json={})
        self.assertEqual(res.status_code, 400)

    def test_post_requires_admin(self):
        def deny():
            raise HTTPException(status_code=403, detail="Admin access required")
        app.dependency_overrides[get_admin_user] = deny
        with patch.object(redownload_module, "run_redownload", AsyncMock()) as run:
            res = self.client.post("/api/v1/redownload/empty-files", json={"all": True})
        self.assertEqual(res.status_code, 403)
        run.assert_not_awaited()


class TestRedownloadCli(unittest.TestCase):
    def test_dry_run_and_yes(self):
        from click.testing import CliRunner
        import main
        from media_manager.web.services import empty_file_redownload as svc
        runner = CliRunner()
        with patch.object(svc, "plan_redownload", AsyncMock(return_value={"items": ITEMS})):
            out = runner.invoke(main.cli, ["redownload-empty"])
        self.assertEqual(out.exit_code, 0, out.output)
        self.assertIn("Dry run", out.output)
        self.assertIn("radarr_search", out.output)
        with patch.object(svc, "run_redownload", AsyncMock(return_value={"results": [dict(ITEMS[0], status="requested")]})):
            out = runner.invoke(main.cli, ["redownload-empty", "--yes"])
        self.assertEqual(out.exit_code, 0, out.output)
        self.assertIn("Done: {'requested': 1}", out.output)


if __name__ == "__main__":
    unittest.main()
