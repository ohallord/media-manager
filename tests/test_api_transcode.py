import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient

from media_manager.metadata import PROBE_VERSION
from media_manager.web.app import app
from media_manager.web.api.auth import get_admin_user, get_current_user
from media_manager.web.services import media_probe_backfill
from media_manager.web.api import transcode as transcode_api

COVERAGE = {"total_items": 10, "pending_items": 4, "probed_items": 6}


class TestProbeBackfillEndpoints(unittest.TestCase):
    def setUp(self):
        self.admin_user = {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        app.dependency_overrides[get_admin_user] = lambda: self.admin_user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_get_status_reports_progress_and_coverage(self):
        with patch.object(media_probe_backfill, "get_coverage", AsyncMock(return_value=COVERAGE)):
            res = self.client.get("/api/v1/transcode/probe-backfill")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["probe_version"], PROBE_VERSION)
        self.assertEqual(data["coverage"], COVERAGE)
        self.assertIn("state", data["status"])
        self.assertIn("running", data)

    def test_post_starts_backfill(self):
        with patch.object(media_probe_backfill, "start_probe_backfill", return_value=True) as mock_start:
            res = self.client.post("/api/v1/transcode/probe-backfill")
        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.json(), {"status": "started"})
        mock_start.assert_called_once()

    def test_post_conflicts_when_already_running(self):
        with patch.object(media_probe_backfill, "start_probe_backfill", return_value=False):
            res = self.client.post("/api/v1/transcode/probe-backfill")
        self.assertEqual(res.status_code, 409)

    def test_rescore_runs_inline(self):
        with patch.object(media_probe_backfill, "rescore_all", AsyncMock(return_value={"total": 3, "changed": 1})):
            res = self.client.post("/api/v1/transcode/rescore")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"total": 3, "changed": 1})

    def test_post_requires_admin(self):
        def deny():
            raise HTTPException(status_code=403, detail="Admin access required")
        app.dependency_overrides[get_admin_user] = deny
        with patch.object(media_probe_backfill, "start_probe_backfill") as mock_start:
            res = self.client.post("/api/v1/transcode/probe-backfill")
        self.assertEqual(res.status_code, 403)
        mock_start.assert_not_called()


class TestTranscodeJobEndpoints(unittest.TestCase):
    def setUp(self):
        self.admin_user = {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        app.dependency_overrides[get_admin_user] = lambda: self.admin_user
        self.client = TestClient(app)
        from media_manager.web.services import transcode_jobs
        self.jobs = transcode_jobs

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_status_and_listing(self):
        from media_manager.web.services import stream_activity
        activity = {"sessions": [], "streams": 2, "video_transcodes": 1, "errors": ["emby"]}
        status = {"counts": {}, "control": {"pause_on": "transcode_only"}, "savings": {"saved_bytes": 500}}
        with patch.object(self.jobs, "get_status", AsyncMock(return_value=status)), \
             patch.object(stream_activity, "get_activity", AsyncMock(return_value=activity)):
            data = self.client.get("/api/v1/transcode/status").json()
        self.assertEqual(data["streams"]["video_transcodes"], 1)
        self.assertIn("retained_originals", data)
        self.assertEqual(data["savings"]["freed_bytes"], 500 - data["retained_originals"]["bytes"])
        self.assertIn("transcode", data["streams"]["pause_reason"])
        with patch.object(self.jobs, "list_jobs", AsyncMock(return_value={"total": 0, "items": []})) as m:
            self.client.get("/api/v1/transcode/jobs?state=done&limit=5")
        self.assertEqual(m.await_args.args[1:], ("done", 5, 0))

    def test_enqueue_and_enqueue_all(self):
        with patch.object(self.jobs, "enqueue", AsyncMock(return_value={"created": ["j"], "skipped": []})) as m:
            res = self.client.post("/api/v1/transcode/jobs", json={"media_item_ids": ["a"]})
        self.assertEqual(res.json()["created"], ["j"])
        self.assertEqual(m.await_args.args[1:], (["a"], True))  # pilot by default
        self.assertEqual(self.client.post("/api/v1/transcode/jobs", json={"media_item_ids": []}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/transcode/jobs/enqueue-all", json={}).status_code, 400)
        with patch.object(self.jobs, "enqueue_all", AsyncMock(return_value={"created_count": 3})):
            res = self.client.post("/api/v1/transcode/jobs/enqueue-all", json={"confirm": True})
        self.assertEqual(res.json(), {"created_count": 3})

    def test_get_requeue_cancel(self):
        job = SimpleNamespace()
        with patch.object(self.jobs, "get_job", AsyncMock(return_value=None)):
            self.assertEqual(self.client.get("/api/v1/transcode/jobs/x").status_code, 404)
        with patch.object(self.jobs, "get_job", AsyncMock(return_value=job)), \
             patch.object(self.jobs, "job_to_dict", return_value={"id": "x"}):
            self.assertEqual(self.client.get("/api/v1/transcode/jobs/x").json(), {"id": "x"})
        for action in ("requeue", "cancel"):
            with patch.object(self.jobs, action, AsyncMock(return_value=job)), \
                 patch.object(self.jobs, "job_to_dict", return_value={"id": "x"}):
                self.assertEqual(self.client.post(f"/api/v1/transcode/jobs/x/{action}").status_code, 200)
            with patch.object(self.jobs, action, AsyncMock(side_effect=self.jobs.JobError("busy"))):
                res = self.client.post(f"/api/v1/transcode/jobs/x/{action}")
            self.assertEqual((res.status_code, res.json()["detail"]), (409, "busy"))


class TestTranscodePlanningEndpoints(unittest.TestCase):
    def setUp(self):
        self.admin_user = {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        app.dependency_overrides[get_admin_user] = lambda: self.admin_user
        self.client = TestClient(app)
        transcode_api._clear_dry_run_cache()
        from media_manager.web.services import transcode_planner
        self.planner = transcode_planner

    def tearDown(self):
        app.dependency_overrides.clear()
        transcode_api._clear_dry_run_cache()
        import asyncio
        from sqlalchemy import delete
        from media_manager.database import AsyncSessionLocal
        from media_manager.models import TranscodeControl, TranscodeSettings

        async def reset():
            async with AsyncSessionLocal() as db:
                await db.execute(delete(TranscodeSettings))
                await db.execute(delete(TranscodeControl))
                await db.commit()
        asyncio.run(reset())

    def test_settings_round_trip_and_validation(self):
        res = self.client.get("/api/v1/transcode/settings")
        self.assertEqual(set(res.json()), {"movie", "tv"})
        res = self.client.put("/api/v1/transcode/settings/movie", json={"cq_1080": 21})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["cq_1080"], 21)
        self.assertEqual(self.client.put("/api/v1/transcode/settings/music", json={}).status_code, 400)
        self.assertEqual(self.client.put("/api/v1/transcode/settings/movie", json={"cq_1080": "x"}).status_code, 400)

    def test_control_update_and_validation(self):
        res = self.client.put("/api/v1/transcode/control",
                              json={"source_codecs": ["H264", "h264", " vc1 ", ""], "excluded_prefixes": ["/mnt/Kids", " "]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["source_codecs"], ["h264", "vc1"])
        self.assertEqual(res.json()["excluded_prefixes"], ["/mnt/Kids"])
        self.assertEqual(self.client.get("/api/v1/transcode/control").json()["source_codecs"], ["h264", "vc1"])
        self.assertEqual(self.client.put("/api/v1/transcode/control", json={"source_codecs": ["hevc"]}).status_code, 400)
        self.assertEqual(self.client.put("/api/v1/transcode/control", json={"excluded_prefixes": ["rel/path"]}).status_code, 400)
        res = self.client.put("/api/v1/transcode/control", json={"run_state": "running", "mode": "pilot"})
        self.assertEqual((res.json()["run_state"], res.json()["mode"]), ("running", "pilot"))
        self.assertEqual(self.client.put("/api/v1/transcode/control", json={"run_state": "go"}).status_code, 400)
        self.assertEqual(self.client.put("/api/v1/transcode/control", json={"mode": "turbo"}).status_code, 400)
        self.assertEqual(self.client.put("/api/v1/transcode/control", json={"pause_on": "any_stream"}).json()["pause_on"], "any_stream")
        self.assertEqual(self.client.put("/api/v1/transcode/control", json={"pause_on": "sometimes"}).status_code, 400)
        res = self.client.put("/api/v1/transcode/control", json={"notify_emails": [" a@x.com ", ""], "failure_streak_threshold": 5})
        self.assertEqual((res.json()["notify_emails"], res.json()["failure_streak_threshold"]), (["a@x.com"], 5))
        self.assertEqual(self.client.put("/api/v1/transcode/control", json={"notify_emails": ["nope"]}).status_code, 400)
        self.assertEqual(self.client.put("/api/v1/transcode/control", json={"failure_streak_threshold": 0}).status_code, 400)

    def test_dry_run_is_cached_until_forced(self):
        report = {"totals": {}, "by_reason": [], "by_bucket": []}
        mock = AsyncMock(return_value=report)
        with patch.object(self.planner, "compute_dry_run_report", mock):
            self.assertEqual(self.client.get("/api/v1/transcode/dry-run").json(), report)
            self.client.get("/api/v1/transcode/dry-run")
            self.assertEqual(mock.await_count, 1)
            self.client.get("/api/v1/transcode/dry-run?force_refresh=true")
            self.assertEqual(mock.await_count, 2)

    def test_candidates_passthrough(self):
        mock = AsyncMock(return_value={"total": 0, "items": []})
        with patch.object(self.planner, "list_candidates", mock):
            res = self.client.get("/api/v1/transcode/candidates?media_type=tv&limit=5&offset=10")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock.await_args.args[1:], ("tv", 5, 10))

    def test_preview_command(self):
        decision = SimpleNamespace(eligible=True, reason="", bucket="1080",
                                   plan={"cq": 22, "preset": "p7", "multipass": "fullres", "ten_bit": True,
                                         "scale_width": None, "decode": "cuda", "deinterlace": False,
                                         "max_video_bps": 1000})
        evaluated = {"item": {"path": "/m/a.mp4"}, "decision": decision}
        probe = {"format": {"format_name": "mov"}, "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {"index": 1, "codec_type": "data", "codec_name": "bin_data"}]}
        with patch.object(self.planner, "get_item_decision", AsyncMock(return_value=evaluated)), \
             patch.object(transcode_api, "run_ffprobe", return_value=probe):
            res = self.client.get("/api/v1/transcode/preview-command/abc")
        data = res.json()
        self.assertTrue(data["eligible"])
        self.assertEqual(data["output_path"], "/m/a.mkv")
        self.assertEqual(data["command"][-1], "/m/a.mkv.mm-partial")
        self.assertEqual(data["dropped_streams"], ["data:bin_data#1"])

    def test_preview_command_ineligible_missing_and_probe_error(self):
        skip = {"item": {"path": "/m/a.mkv"}, "decision": SimpleNamespace(eligible=False, reason="hdr")}
        with patch.object(self.planner, "get_item_decision", AsyncMock(return_value=skip)):
            data = self.client.get("/api/v1/transcode/preview-command/abc").json()
        self.assertEqual(data, {"eligible": False, "reason": "hdr", "reason_label": "HDR / Dolby Vision"})
        with patch.object(self.planner, "get_item_decision", AsyncMock(return_value=None)):
            self.assertEqual(self.client.get("/api/v1/transcode/preview-command/abc").status_code, 404)
        ok = {"item": {"path": "/m/a.mkv"}, "decision": SimpleNamespace(eligible=True, plan={}, bucket="1080")}
        with patch.object(self.planner, "get_item_decision", AsyncMock(return_value=ok)), \
             patch.object(transcode_api, "run_ffprobe", side_effect=RuntimeError("gone")):
            self.assertEqual(self.client.get("/api/v1/transcode/preview-command/abc").status_code, 409)


if __name__ == "__main__":
    unittest.main()
