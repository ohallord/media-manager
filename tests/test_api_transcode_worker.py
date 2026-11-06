import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from media_manager.transcode.protocol import PROTOCOL_VERSION
from media_manager.web.app import app
from media_manager.web.api import transcode_worker as api

TOKEN = "s3cret-token"
HEADERS = {"X-Transcode-Token": TOKEN, "X-Transcode-Protocol": str(PROTOCOL_VERSION)}
JOB = {"id": "j1", "state": "claimed", "media_type": "tv"}


class WorkerApiBase(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TRANSCODE_WORKER_TOKEN": TOKEN})
        self.env.start()
        self.client = TestClient(app)
        self.control = SimpleNamespace(run_state="running", mode="full", pause_on="transcode_only")
        self.activity = {"sessions": [], "streams": 0, "video_transcodes": 0, "errors": []}
        self.patches = [
            patch.object(api.stream_activity, "get_activity", AsyncMock(side_effect=lambda db, force=False: self.activity)),
            patch.object(api.planner, "get_or_create_control", AsyncMock(return_value=self.control)),
            patch.object(api.planner, "get_settings", AsyncMock(return_value={"cq_1080": 25})),
            patch.object(api.jobs, "job_to_dict", side_effect=lambda j: JOB),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()


class TestWorkerAuth(WorkerApiBase):
    def test_missing_server_token_is_503(self):
        with patch.dict(os.environ, {"TRANSCODE_WORKER_TOKEN": ""}):
            res = self.client.post("/api/v1/transcode/worker/claim", json={"worker_id": "w"}, headers=HEADERS)
        self.assertEqual(res.status_code, 503)

    def test_bad_token_and_protocol(self):
        res = self.client.post("/api/v1/transcode/worker/claim", json={"worker_id": "w"},
                               headers=dict(HEADERS, **{"X-Transcode-Token": "wrong"}))
        self.assertEqual(res.status_code, 401)
        res = self.client.post("/api/v1/transcode/worker/claim", json={"worker_id": "w"},
                               headers={"X-Transcode-Protocol": "1"})
        self.assertEqual(res.status_code, 401)
        res = self.client.post("/api/v1/transcode/worker/claim", json={"worker_id": "w"},
                               headers=dict(HEADERS, **{"X-Transcode-Protocol": str(PROTOCOL_VERSION + 5)}))
        self.assertEqual(res.status_code, 409)

    def test_non_ascii_token_rejected_not_crash(self):
        with patch.object(api.hmac, "compare_digest", side_effect=TypeError("non-ascii")):
            res = self.client.post("/api/v1/transcode/worker/claim", json={"worker_id": "w"}, headers=HEADERS)
        self.assertEqual(res.status_code, 401)


class TestWorkerEndpoints(WorkerApiBase):
    def test_heartbeat_reports_pause_and_version(self):
        with patch.object(api.jobs, "record_heartbeat", AsyncMock()) as rec:
            data = self.client.post("/api/v1/transcode/worker/heartbeat",
                                    json={"worker_id": "w", "version": "1.0", "detail": {"pid": 1}}, headers=HEADERS).json()
        rec.assert_awaited_once()
        self.assertEqual((data["pause"], data["protocol"]), (False, PROTOCOL_VERSION))
        self.assertIn("server_version", data)
        self.assertEqual(data["settings"]["movie"], {"cq_1080": 25})
        self.control.run_state = "paused"
        with patch.object(api.jobs, "record_heartbeat", AsyncMock()):
            data = self.client.post("/api/v1/transcode/worker/heartbeat", json={"worker_id": "w"}, headers=HEADERS).json()
        self.assertEqual((data["pause"], data["reason"]), (True, "paused by admin"))

    def test_claim_job_or_204(self):
        with patch.object(api.jobs, "claim", AsyncMock(return_value=None)):
            res = self.client.post("/api/v1/transcode/worker/claim", json={"worker_id": "w"}, headers=HEADERS)
        self.assertEqual(res.status_code, 204)
        self.assertIn("X-Server-Version", res.headers)
        with patch.object(api.jobs, "claim", AsyncMock(return_value=SimpleNamespace(media_type="tv"))):
            data = self.client.post("/api/v1/transcode/worker/claim", json={"worker_id": "w"}, headers=HEADERS).json()
        self.assertEqual((data["job"], data["settings"]), (JOB, {"cq_1080": 25}))

    def test_inflight(self):
        with patch.object(api.jobs, "inflight", AsyncMock(return_value=[object()])):
            data = self.client.get("/api/v1/transcode/worker/inflight?worker_id=w", headers=HEADERS).json()
        self.assertEqual(data["jobs"], [JOB])

    def test_progress_phase_complete_fail_and_conflicts(self):
        calls = [
            ("progress", "report_progress", {"worker_id": "w", "progress_pct": 5}, {"cancel": False}),
            ("phase", "set_phase", {"worker_id": "w", "phase": "encoding"}, object()),
            ("complete", "complete", {"worker_id": "w", "output_path": "/o.mkv", "output_size": 1},
             SimpleNamespace(source_path="/i.mp4", output_path="/o.mkv")),
            ("fail", "fail", {"worker_id": "w", "error_code": "not_smaller"}, object()),
        ]
        quiet = patch.object(api, "notify_media_servers", AsyncMock())
        quiet.start()
        self.addCleanup(quiet.stop)
        for endpoint, fn, body, result in calls:
            with self.subTest(endpoint=endpoint):
                with patch.object(api.jobs, fn, AsyncMock(return_value=result)):
                    res = self.client.post(f"/api/v1/transcode/worker/jobs/j1/{endpoint}", json=body, headers=HEADERS)
                self.assertEqual(res.status_code, 200)
                with patch.object(api.jobs, fn, AsyncMock(side_effect=api.jobs.JobError("nope"))):
                    res = self.client.post(f"/api/v1/transcode/worker/jobs/j1/{endpoint}", json=body, headers=HEADERS)
                self.assertEqual((res.status_code, res.json()["detail"]), (409, "nope"))
        with patch.object(api.jobs, "report_progress", AsyncMock(return_value={"cancel": True})):
            data = self.client.post("/api/v1/transcode/worker/jobs/j1/progress", json={"worker_id": "w"}, headers=HEADERS).json()
        self.assertEqual((data["cancel"], data["pause"]), (True, False))


class TestWorkerStreamAwareness(WorkerApiBase):
    def test_heartbeat_pauses_for_media_server_transcodes(self):
        self.activity = {"sessions": [], "streams": 1, "video_transcodes": 1, "errors": []}
        with patch.object(api.jobs, "record_heartbeat", AsyncMock()):
            data = self.client.post("/api/v1/transcode/worker/heartbeat", json={"worker_id": "w"}, headers=HEADERS).json()
        self.assertTrue(data["pause"])
        self.assertIn("transcode", data["reason"])

    def test_progress_ignores_mode_off_but_honours_pause_click(self):
        self.control.mode = "off"
        with patch.object(api.jobs, "report_progress", AsyncMock(return_value={"cancel": False})):
            data = self.client.post("/api/v1/transcode/worker/jobs/j1/progress", json={"worker_id": "w"}, headers=HEADERS).json()
            self.assertFalse(data["pause"])
            self.control.run_state = "paused"
            data = self.client.post("/api/v1/transcode/worker/jobs/j1/progress", json={"worker_id": "w"}, headers=HEADERS).json()
        self.assertTrue(data["pause"])

    def test_swap_refused_while_file_is_playing(self):
        self.activity = {"sessions": [{"path": "/m/a.mp4"}], "streams": 1, "video_transcodes": 0, "errors": []}
        with patch.object(api.jobs, "get_job", AsyncMock(return_value=SimpleNamespace(source_path="/m/a.mp4"))), \
             patch.object(api.stream_activity, "is_playing", return_value=True), \
             patch.object(api.jobs, "set_phase", AsyncMock()) as set_phase:
            res = self.client.post("/api/v1/transcode/worker/jobs/j1/phase",
                                   json={"worker_id": "w", "phase": "swapping"}, headers=HEADERS)
        self.assertEqual(res.status_code, 423)
        set_phase.assert_not_awaited()

    def test_complete_notifies_media_servers_once(self):
        done_job = SimpleNamespace(source_path="/m/a.mp4", output_path="/m/a.mkv", state="done")
        body = {"worker_id": "w", "output_path": "/m/a.mkv", "output_size": 1}
        with patch.object(api.jobs, "get_job", AsyncMock(return_value=SimpleNamespace(state="running"))), \
             patch.object(api.jobs, "complete", AsyncMock(return_value=done_job)), \
             patch.object(api, "notify_media_servers", AsyncMock()) as notify:
            self.client.post("/api/v1/transcode/worker/jobs/j1/complete", json=body, headers=HEADERS)
        notify.assert_awaited_once_with("/m/a.mp4", "/m/a.mkv")
        with patch.object(api.jobs, "get_job", AsyncMock(return_value=done_job)), \
             patch.object(api.jobs, "complete", AsyncMock(return_value=done_job)), \
             patch.object(api, "notify_media_servers", AsyncMock()) as notify:
            self.client.post("/api/v1/transcode/worker/jobs/j1/complete", json=body, headers=HEADERS)
        notify.assert_not_awaited()  # idempotent resend: no duplicate refresh


if __name__ == "__main__":
    unittest.main()
