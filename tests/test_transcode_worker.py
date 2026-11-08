import errno
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from media_manager.transcode import fsops, worker as worker_mod
from media_manager.transcode.client import ApiError
from media_manager.transcode.worker import JobFailed, Worker, WorkerConfig

SETTINGS = {"min_savings_pct": 10, "retain_originals_days": 7}
HAVE_X265 = bool(shutil.which("ffmpeg")) and "libx265" in subprocess.run(
    ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout if shutil.which("ffmpeg") else False


class FakeClient:
    def __init__(self, claims=(), inflight=(), pause=False, server_version="1.0"):
        self.calls = []
        self.claims = list(claims)
        self._inflight = list(inflight)
        self.pause = pause
        self.server_version = server_version
        self.progress_resp = {}

    def heartbeat(self, detail=None):
        self.calls.append(("heartbeat",))
        return {"pause": self.pause, "settings": {"movie": dict(SETTINGS)}}

    def claim(self):
        self.calls.append(("claim",))
        return self.claims.pop(0) if self.claims else None

    def inflight(self):
        return self._inflight

    def progress(self, job_id, *args):
        self.calls.append(("progress", job_id) + args)
        r = self.progress_resp
        return r() if callable(r) else r

    def phase(self, job_id, phase, **paths):
        self.calls.append(("phase", job_id, phase, paths))
        return {}

    def complete(self, job_id, payload):
        self.calls.append(("complete", job_id, payload))
        return {}

    def fail(self, job_id, code, message=""):
        self.calls.append(("fail", job_id, code, message))
        return {}

    def phases(self):
        return [c[2] for c in self.calls if c[0] == "phase"]

    def failures(self):
        return [(c[2], c[3]) for c in self.calls if c[0] == "fail"]

    def completions(self):
        return [c[2] for c in self.calls if c[0] == "complete"]


class WorkerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mm_worker_")
        self.lib = os.path.join(self.tmp, "lib")
        self.scratch = os.path.join(self.tmp, "scratch")
        os.makedirs(os.path.join(self.lib, "Movie"))
        with open(os.path.join(self.lib, ".keep"), "w") as fh:
            fh.write("x")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg(self, **kw):
        base = dict(api_url="http://app", token="t", scratch_dir=self.scratch, worker_id="w", version="1.0",
                    encoder="libx265", media_dirs=[self.lib], source_dirs=[], uid=os.getuid(), gid=os.getgid(),
                    idle_sleep=0, paused_sleep=0, poll_interval=0, progress_interval=0, sample_check=False)
        base.update(kw)
        return WorkerConfig(**base)

    def make_worker(self, client, **kw):
        w = Worker(self.cfg(**kw.pop("cfg", {})), client, sleep=lambda s: None,
                   network_check=kw.pop("network_check", lambda p: False), **kw)
        os.makedirs(self.scratch, exist_ok=True)
        return w

    def job(self, src, **kw):
        st = os.stat(src)
        base = {"id": "job12345abcdef", "title": "Movie", "media_type": "movie", "source_path": src,
                "source_size": st.st_size, "source_mtime": st.st_mtime,
                "plan": {"cq": 30, "preset": "p5", "multipass": "qres", "ten_bit": True, "scale_width": None,
                         "decode": "sw", "deinterlace": False, "max_video_bps": 8_000_000}}
        base.update(kw)
        return base


def make_source(path, seconds=4):
    """Real h264 MP4 at a deliberately bloated bitrate: 2 audio tracks + a mov_text subtitle."""
    srt = path + ".srt"
    with open(srt, "w") as fh:
        fh.write("1\n00:00:00,500 --> 00:00:02,000\nHello\n")
    subprocess.run([
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=24:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}",
        "-i", srt, "-map", "0", "-map", "1", "-map", "2", "-map", "3",
        "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "8M", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-c:s", "mov_text", "-metadata:s:a:1", "language=fre", path,
    ], check=True)
    os.remove(srt)


@unittest.skipUnless(HAVE_X265, "needs ffmpeg with libx265")
class TestEndToEnd(WorkerTestBase):
    def test_full_pipeline_swaps_in_verified_hevc(self):
        src = os.path.join(self.lib, "Movie", "Movie (2000).mp4")
        make_source(src)
        client = FakeClient(claims=[{"job": self.job(src), "settings": dict(SETTINGS)}])
        w = self.make_worker(client)
        self.assertEqual(w.run_forever(once=True), "done")

        self.assertEqual(client.failures(), [])
        self.assertEqual(client.phases(), ["preparing", "encoding", "verifying", "installing", "installing",
                                           "swapping", "swapped"])
        out = os.path.join(self.lib, "Movie", "Movie (2000).mkv")
        backup = fsops.backup_path_for(src)
        self.assertTrue(os.path.exists(out))
        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.exists(backup))  # retained for retain_originals_days
        payload = client.completions()[0]
        self.assertEqual((payload["output_path"], payload["backup_path"]), (out, backup))
        self.assertEqual(payload["output_hash"], fsops.hash_file(out))
        probe = payload["probe"]
        kinds = [s["codec_type"] for s in probe["streams"]]
        self.assertEqual(kinds, ["video", "audio", "audio", "subtitle"])
        video = probe["streams"][0]
        self.assertEqual((video["codec_name"], video["pix_fmt"]), ("hevc", "yuv420p10le"))
        self.assertEqual(probe["streams"][3]["codec_name"], "subrip")
        self.assertEqual(probe["streams"][2]["tags"]["language"], "fre")
        self.assertEqual(os.listdir(self.scratch), [])  # scratch cleaned
        install = [c for c in client.calls if c[0] == "phase" and c[2] == "installing"]
        self.assertEqual(install[1][3]["output_hash"], payload["output_hash"])  # hash announced before swap

    def test_mkv_source_same_path_and_no_retention(self):
        mp4 = os.path.join(self.lib, "Movie", "tmp.mp4")
        make_source(mp4)
        src = os.path.join(self.lib, "Movie", "Same.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", mp4, "-map", "0", "-c", "copy", "-c:s", "srt", src], check=True)
        os.remove(mp4)
        settings = dict(SETTINGS, retain_originals_days=0)
        client = FakeClient(claims=[{"job": self.job(src), "settings": settings}])
        self.make_worker(client).run_forever(once=True)
        self.assertEqual(client.failures(), [])
        self.assertTrue(os.path.exists(src))
        self.assertFalse(os.path.exists(fsops.backup_path_for(src)))
        self.assertIsNone(client.completions()[0]["backup_path"])

    def test_not_smaller_keeps_original(self):
        src = os.path.join(self.lib, "Movie", "Movie (2000).mp4")
        make_source(src)
        client = FakeClient(claims=[{"job": self.job(src), "settings": dict(SETTINGS, min_savings_pct=99.9)}])
        self.make_worker(client).run_forever(once=True)
        self.assertEqual(client.failures()[0][0], "not_smaller")
        self.assertTrue(os.path.exists(src))
        self.assertEqual(sorted(os.listdir(os.path.join(self.lib, "Movie"))), ["Movie (2000).mp4"])

    def test_network_source_is_copied_to_scratch(self):
        src = os.path.join(self.lib, "Movie", "Movie (2000).mp4")
        make_source(src)
        client = FakeClient(claims=[{"job": self.job(src), "settings": dict(SETTINGS)}])
        w = self.make_worker(client, network_check=lambda p: True)
        with patch.object(fsops, "copy_with_hash", wraps=fsops.copy_with_hash) as copy:
            w.run_forever(once=True)
        self.assertEqual(client.failures(), [])
        self.assertTrue(copy.call_args_list[0].args[1].startswith(self.scratch))

    def test_sample_check_skips_hopeless_files(self):
        src = os.path.join(self.lib, "Movie", "Movie (2000).mp4")
        make_source(src, seconds=4)
        job = self.job(src)
        client = FakeClient(claims=[{"job": job, "settings": dict(SETTINGS, min_savings_pct=95)}])
        w = self.make_worker(client, cfg={"sample_check": True})
        with patch.object(worker_mod, "SAMPLE_MIN_DURATION", 1), patch.object(worker_mod, "SAMPLE_SECONDS", 1):
            w.run_forever(once=True)
        self.assertEqual(client.failures()[0][0], "predicted_not_smaller")
        self.assertNotIn("encoding", client.phases())

    def test_recovery_after_crash_between_renames(self):
        src = os.path.join(self.lib, "Movie", "Movie (2000).mp4")
        make_source(src)
        # Simulate: encode + install done, source renamed to backup, crash before temp -> output.
        out = fsops.output_path_for(src)
        temp = fsops.temp_path_for(out, "job12345abcdef")
        shutil.copy(src, temp)
        digest = fsops.hash_file(temp)
        os.rename(src, fsops.backup_path_for(src))
        job = dict(self.job(fsops.backup_path_for(src)), source_path=src, phase="swapping", temp_path=temp,
                   output_path=out, backup_path=fsops.backup_path_for(src), output_hash=digest)
        client = FakeClient(inflight=[job])
        w = self.make_worker(client)
        open(os.path.join(self.scratch, "job12345abcdef.mkv"), "w").close()  # leftover scratch file
        self.assertEqual(w.run_forever(once=True), "idle")
        self.assertTrue(os.path.exists(out))
        self.assertFalse(os.path.exists(temp))
        self.assertEqual(client.phases(), ["swapped"])
        self.assertEqual(client.completions()[0]["output_path"], out)
        self.assertEqual(os.listdir(self.scratch), [])


class TestPreflightAndFailures(WorkerTestBase):
    def _src(self, name="Movie (2000).mp4", data=b"x" * 1000):
        path = os.path.join(self.lib, "Movie", name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def _process(self, job, settings=None, **kw):
        client = FakeClient()
        w = self.make_worker(client, **kw)
        w.process(job, settings or dict(SETTINGS))
        return client

    def test_preflight_failures(self):
        src = self._src()
        cases = {
            "source_missing": dict(source_path=os.path.join(self.lib, "Movie", "gone.mp4")),
            "source_changed": dict(source_size=1),
        }
        for code, override in cases.items():
            with self.subTest(code=code):
                self.assertEqual(self._process(dict(self.job(src), **override)).failures()[0][0], code)
        with patch.object(fsops, "volume_ok", return_value=False):
            self.assertEqual(self._process(self.job(src)).failures()[0][0], "volume_unhealthy")
        with patch.object(fsops, "can_write_dir", side_effect=lambda p: p == self.scratch):
            self.assertEqual(self._process(self.job(src)).failures()[0][0], "permission_denied")
        open(os.path.join(self.lib, "Movie", "Movie (2000).mkv"), "w").close()
        self.assertEqual(self._process(self.job(src)).failures()[0][0], "not_eligible")
        os.remove(os.path.join(self.lib, "Movie", "Movie (2000).mkv"))
        with patch.object(fsops, "free_bytes", return_value=0):
            self.assertEqual(self._process(self.job(src)).failures()[0][0], "disk_full")
        with patch.object(fsops, "free_bytes", side_effect=OSError("io")):
            self.assertEqual(self._process(self.job(src)).failures()[0][0], "volume_unhealthy")

    def test_probe_killed_by_stop_is_shutdown_not_corrupt(self):
        # SIGTERM reaches ffprobe too; that must requeue the job, not skip it as source_corrupt.
        src = self._src()
        client = FakeClient()
        w = self.make_worker(client)

        def probe(*a, **k):
            w.request_stop()
            raise RuntimeError("ffprobe failed (-15):")
        with patch.object(worker_mod.runner, "probe", side_effect=probe):
            w.process(self.job(src), dict(SETTINGS))
        self.assertEqual(client.failures()[0][0], "worker_shutdown")

    def test_probe_and_copy_failures(self):
        src = self._src()
        with patch.object(worker_mod.runner, "probe", side_effect=RuntimeError("bad file")):
            self.assertEqual(self._process(self.job(src)).failures()[0][0], "source_corrupt")
        with patch.object(worker_mod.runner, "probe", return_value={"streams": [{"codec_type": "audio"}]}):
            self.assertEqual(self._process(self.job(src)).failures()[0][0], "unsupported_stream")
        with patch.object(fsops, "copy_with_hash", side_effect=OSError(errno.ENOSPC, "full")):
            client = self._process(self.job(src), network_check=lambda p: True)
        self.assertEqual(client.failures()[0][0], "disk_full")
        with patch.object(fsops, "copy_with_hash", side_effect=OSError(errno.EIO, "io")):
            client = self._process(self.job(src), network_check=lambda p: True)
        self.assertEqual(client.failures()[0][0], "copy_failed")

    def test_unexpected_errors_are_reported_and_report_errors_swallowed(self):
        src = self._src()
        with patch.object(Worker, "_process", side_effect=ValueError("boom")):
            client = self._process(self.job(src))
        self.assertEqual(client.failures()[0][0], "encoder_crash")
        client = FakeClient()
        client.fail = MagicMock(side_effect=ConnectionError("down"))
        w = self.make_worker(client)
        with patch.object(Worker, "_process", side_effect=JobFailed("copy_failed", "x")):
            w.process(self.job(src), dict(SETTINGS))  # must not raise

    def test_swap_failures_roll_back(self):
        src = self._src()
        temp = os.path.join(self.lib, "Movie", ".t.mm-partial")
        out = os.path.join(self.lib, "Movie", "Movie (2000).mkv")
        open(temp, "w").close()
        w = self.make_worker(FakeClient())
        real_rename = os.rename

        def second_fails(a, b):
            if a == temp:
                raise OSError(errno.EIO, "io")
            real_rename(a, b)
        with patch.object(worker_mod.os, "rename", side_effect=second_fails):
            with self.assertRaises(JobFailed) as ctx:
                w._swap(src, fsops.backup_path_for(src), temp, out)
        self.assertEqual(ctx.exception.code, "copy_failed")
        self.assertTrue(os.path.exists(src))  # original restored
        self.assertFalse(os.path.exists(temp))
        open(temp, "w").close()
        with patch.object(worker_mod.os, "rename", side_effect=OSError(errno.EACCES, "denied")):
            with self.assertRaises(JobFailed) as ctx:
                w._swap(src, fsops.backup_path_for(src), temp, out)
        self.assertEqual(ctx.exception.code, "permission_denied")


class FakeProc:
    """Stands in for runner.FfmpegProcess: runs for `ticks` polls, then exits with rc."""

    def __init__(self, ticks, rc=0, errors="", progress=None):
        self.ticks, self.rc, self.errors = ticks, rc, errors
        self.progress = progress or {"out_time_us": "5000000", "fps": "300", "speed": "10x"}
        self.paused = False
        self.events = []
        self.on_progress = None

    def __call__(self, args, on_progress=None, popen=None):
        self.args = args
        self.on_progress = on_progress
        return self

    def start(self):
        self.on_progress(self.progress)
        return self

    def poll(self):
        if self.ticks <= 0:
            return self.rc
        self.ticks -= 1
        return None

    def wait(self, timeout=None):
        return self.rc

    def pause(self):
        self.paused = True
        self.events.append("pause")

    def resume(self):
        self.paused = False
        self.events.append("resume")

    def terminate(self, grace=10):
        self.events.append("terminate")


PROBE = {"format": {"duration": "10"}, "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}]}
PLAN = {"cq": 25, "decode": "cuda", "ten_bit": True}


class TestEncodeLoop(WorkerTestBase):
    def _worker(self, client, clock_step=1, gpu_infos=None, **cfg):
        t = {"now": 0}

        def clock():
            t["now"] += clock_step
            return t["now"]
        infos = list(gpu_infos or [])
        gq = (lambda: infos.pop(0) if infos else None)
        return Worker(self.cfg(encoder="hevc_nvenc", **cfg), client, sleep=lambda s: None, clock=clock,
                      gpu_query=gq, network_check=lambda p: False)

    def test_progress_reports_eta_and_completes(self):
        client = FakeClient()
        proc = FakeProc(ticks=2)
        with patch.object(worker_mod.runner, "FfmpegProcess", proc):
            self._worker(client)._run_encode({"id": "j"}, PROBE, PLAN, "/in", "/out", force_sw=False)
        reports = [c for c in client.calls if c[0] == "progress"]
        self.assertEqual(reports[0][1:], ("j", 50.0, 300.0, 0))
        self.assertIn("-hwaccel", proc.args)

    def test_cancel_stop_and_lease_loss(self):
        for resp, code in (({"cancel": True}, "worker_shutdown"),):
            client = FakeClient()
            client.progress_resp = resp
            proc = FakeProc(ticks=5)
            with patch.object(worker_mod.runner, "FfmpegProcess", proc), self.assertRaises(JobFailed) as ctx:
                self._worker(client)._run_encode({"id": "j"}, PROBE, PLAN, "/in", "/out", force_sw=False)
            self.assertEqual(ctx.exception.code, code)
            self.assertIn("terminate", proc.events)
        client = FakeClient()
        w = self._worker(client)
        w.stop_requested = True
        with patch.object(worker_mod.runner, "FfmpegProcess", FakeProc(ticks=5)), self.assertRaises(JobFailed) as ctx:
            w._run_encode({"id": "j"}, PROBE, PLAN, "/in", "/out", force_sw=False)
        self.assertEqual(ctx.exception.code, "worker_shutdown")

        def lost():
            raise ApiError(409, "leased to another worker")
        client = FakeClient()
        client.progress_resp = lost
        with patch.object(worker_mod.runner, "FfmpegProcess", FakeProc(ticks=5)), self.assertRaises(JobFailed) as ctx:
            self._worker(client)._run_encode({"id": "j"}, PROBE, PLAN, "/in", "/out", force_sw=False)
        self.assertEqual(ctx.exception.code, "worker_lost")

        def server_down():
            raise ApiError(500, "boom")
        client = FakeClient()
        client.progress_resp = server_down
        with patch.object(worker_mod.runner, "FfmpegProcess", FakeProc(ticks=5)), self.assertRaises(ApiError):
            self._worker(client)._run_encode({"id": "j"}, PROBE, PLAN, "/in", "/out", force_sw=False)

    def test_idle_foreign_session_while_paused_is_not_contention(self):
        # Paused by the server; meanwhile nvidia-smi sees our stopped session + an idle Plex one.
        client = FakeClient()
        responses = [{"pause": True}] * 3 + [{"pause": False}] * 400
        client.progress_resp = lambda: responses.pop(0)
        idle_plex = {"encoder_sessions": 2, "encoder_util": 0, "decoder_util": 0, "mem_free_mb": 9000}
        proc = FakeProc(ticks=10)
        w = self._worker(client, gpu_infos=[idle_plex] * 50, gpu_check_interval=1)
        with patch.object(worker_mod.runner, "FfmpegProcess", proc):
            w._run_encode({"id": "j"}, PROBE, PLAN, "/in", "/out", force_sw=False)
        self.assertEqual(proc.events, ["pause", "resume"])

    def test_pauses_for_gpu_contention_and_resumes_after_quiet_period(self):
        client = FakeClient()
        busy = {"encoder_sessions": 2, "encoder_util": 80, "decoder_util": 0, "mem_free_mb": 9000}
        idle = {"encoder_sessions": 0, "encoder_util": 0, "decoder_util": 0, "mem_free_mb": 9000}
        proc = FakeProc(ticks=400)
        w = self._worker(client, gpu_infos=[busy] + [idle] * 50, gpu_check_interval=1)
        with patch.object(worker_mod.runner, "FfmpegProcess", proc):
            w._run_encode({"id": "j"}, PROBE, PLAN, "/in", "/out", force_sw=False)
        self.assertEqual(proc.events, ["pause", "resume"])

    def test_server_pause_and_pause_timeout(self):
        client = FakeClient()
        client.progress_resp = {"pause": True}
        proc = FakeProc(ticks=100)
        w = self._worker(client, clock_step=100, max_pause_seconds=250)
        with patch.object(worker_mod.runner, "FfmpegProcess", proc), self.assertRaises(JobFailed) as ctx:
            w._run_encode({"id": "j"}, PROBE, PLAN, "/in", "/out", force_sw=False)
        self.assertEqual(ctx.exception.code, "paused_too_long")
        self.assertEqual(proc.events[0], "pause")

    def test_failures_gpu_busy_and_software_fallback(self):
        client = FakeClient()
        with patch.object(worker_mod.runner, "FfmpegProcess", FakeProc(0, rc=1, errors="OpenEncodeSessionEx failed")), \
             self.assertRaises(JobFailed) as ctx:
            self._worker(client)._run_encode({"id": "j"}, PROBE, PLAN, "/in", "/out", force_sw=False)
        self.assertEqual(ctx.exception.code, "gpu_busy")

        procs = [FakeProc(0, rc=1, errors="cuvid decode error"), FakeProc(0, rc=0)]
        factory = MagicMock(side_effect=lambda args, on_progress=None, popen=None: procs.pop(0)(args, on_progress))
        with patch.object(worker_mod.runner, "FfmpegProcess", factory):
            self._worker(client)._encode({"id": "j", "title": "T"}, PROBE, PLAN, "/in", "/out")
        second_args = factory.call_args_list[1].args[0]
        self.assertNotIn("-hwaccel", second_args)  # retried with software decode

        procs = [FakeProc(0, rc=1, errors="x"), FakeProc(0, rc=1, errors="still broken")]
        factory = MagicMock(side_effect=lambda args, on_progress=None, popen=None: procs.pop(0)(args, on_progress))
        with patch.object(worker_mod.runner, "FfmpegProcess", factory), self.assertRaises(JobFailed) as ctx:
            self._worker(client)._encode({"id": "j", "title": "T"}, PROBE, PLAN, "/in", "/out")
        self.assertIn("still broken", ctx.exception.message)
        with patch.object(worker_mod.runner, "FfmpegProcess", FakeProc(0, rc=1, errors="x")), self.assertRaises(JobFailed):
            self._worker(client)._encode({"id": "j", "title": "T"}, PROBE, dict(PLAN, decode="sw"), "/in", "/out")

    def test_bad_progress_values(self):
        client = FakeClient()
        w = self._worker(client)
        w._report_progress("j", {"out_time_us": "N/A", "fps": "x", "speed": "N/Ax"}, 10)
        self.assertEqual(client.calls[-1][1:], ("j", None, None, None))


class TestAnnounceSwap(WorkerTestBase):
    def test_waits_while_file_is_played_then_swaps(self):
        client = FakeClient()
        responses = [ApiError(423, "playing"), ApiError(423, "playing"), {}]

        def phase(job_id, phase, **paths):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            client.calls.append(("phase", job_id, phase, paths))
            return r
        client.phase = phase
        self.make_worker(client)._announce_swap("j")
        self.assertEqual(client.phases(), ["swapping"])
        self.assertEqual(len([c for c in client.calls if c[0] == "progress"]), 2)  # lease kept alive

    def test_gives_up_after_max_pause_and_reraises_other_errors(self):
        client = FakeClient()
        client.phase = MagicMock(side_effect=ApiError(423, "playing"))
        w = self.make_worker(client, cfg={"max_pause_seconds": 120})
        with self.assertRaises(JobFailed) as ctx:
            w._announce_swap("j")
        self.assertEqual(ctx.exception.code, "paused_too_long")
        client.phase = MagicMock(side_effect=ApiError(409, "not yours"))
        with self.assertRaises(JobFailed) as ctx:
            w._announce_swap("j")
        self.assertEqual(ctx.exception.code, "worker_lost")
        client.phase = MagicMock(side_effect=ApiError(400, "bad phase"))
        with self.assertRaises(ApiError):
            w._announce_swap("j")


class TestLeaseKeeper(WorkerTestBase):
    """Long silent steps (copy, probe, verify decode, install) must not let the 180 s lease lapse."""

    def _keeper(self, client, interval=0, check=0.005):
        return worker_mod.LeaseKeeper(client, "job12345abcdef", interval, check)

    def _wait_for(self, cond):
        import time
        deadline = time.monotonic() + 5
        while not cond() and time.monotonic() < deadline:
            time.sleep(0.005)
        return cond()

    def test_renews_in_background_while_main_thread_is_busy(self):
        client = FakeClient()
        keeper = self._keeper(client).start()
        try:
            self.assertTrue(self._wait_for(lambda: len(client.calls) >= 2))
        finally:
            keeper.stop()
        self.assertEqual(client.calls[0], ("progress", "job12345abcdef"))
        self.assertIsNone(keeper.lost)

    def test_quiet_while_main_thread_reports(self):
        client = FakeClient()
        keeper = self._keeper(client, interval=3600).start()
        keeper.touch()
        keeper.stop()
        self.assertEqual(client.calls, [])

    def test_lost_lease_and_cancel_stop_the_keeper(self):
        def lost():
            raise ApiError(409, "leased to another worker")
        client = FakeClient()
        client.progress_resp = lost
        keeper = self._keeper(client)
        keeper.renew()
        self.assertEqual(keeper.lost.code, "worker_lost")
        self.assertTrue(keeper._stop.is_set())

        client = FakeClient()
        client.progress_resp = {"cancel": True}
        keeper = self._keeper(client)
        keeper.renew()
        self.assertEqual(keeper.lost.code, "worker_shutdown")

    def test_transient_errors_are_retried_not_fatal(self):
        for exc in (ApiError(502, "bad gateway"), ConnectionError("down")):
            def boom(exc=exc):
                raise exc
            client = FakeClient()
            client.progress_resp = boom
            keeper = self._keeper(client)
            with self.assertLogs("media_manager.transcode.worker", "WARNING"):
                keeper.renew()
            self.assertIsNone(keeper.lost)
            self.assertFalse(keeper._stop.is_set())

    def test_phase_raises_when_lease_was_lost_and_on_409(self):
        client = FakeClient()
        w = self.make_worker(client)
        w._keep_lease("j")
        try:
            w._lease.lost = JobFailed("worker_lost", "gone")
            with self.assertRaises(JobFailed) as ctx:
                w._phase("j", "installing")
            self.assertEqual(ctx.exception.code, "worker_lost")
            self.assertEqual(client.phases(), [])  # never told the server about a phase it won't accept
        finally:
            w._release_lease()
        w._release_lease()  # idempotent
        client.phase = MagicMock(side_effect=ApiError(409, "Job is leased to another worker"))
        with self.assertRaises(JobFailed) as ctx:
            w._phase("j", "verifying")
        self.assertEqual(ctx.exception.code, "worker_lost")

    def test_process_reports_lost_lease_instead_of_crashing(self):
        client = FakeClient()
        w = self.make_worker(client)

        def slow_verify(job, settings, scratch):
            w._lease.lost = JobFailed("worker_lost", "server rejected lease renewal")
            w._phase(job["id"], "installing")
        with patch.object(Worker, "_process", side_effect=slow_verify):
            w.process({"id": "j", "title": "Big"}, dict(SETTINGS))
        self.assertEqual(client.failures(), [("worker_lost", "server rejected lease renewal")])
        self.assertIsNone(w._lease)

    def test_encode_progress_and_phases_touch_the_lease(self):
        client = FakeClient()
        w = self.make_worker(client)
        w._keep_lease("j")
        try:
            w._lease.renewed_at = -1e9
            w._phase("j", "encoding")
            self.assertGreater(w._lease.renewed_at, 0)
            w._lease.renewed_at = -1e9
            w._report_progress("j", {"out_time_us": "1000000"}, 10.0)
            self.assertGreater(w._lease.renewed_at, 0)
        finally:
            w._release_lease()


class TestSampleCheckAndDecode(WorkerTestBase):
    def test_sample_check_edges(self):
        w = self.make_worker(FakeClient())
        job = {"id": "j", "title": "T", "source_size": 1000}
        probe = {"format": {"duration": "1000"}, "streams": [{"index": 0, "codec_type": "video"}]}
        self.assertIsNone(w._sample_check(job, SETTINGS, {"format": {"duration": "10"}}, {"max_video_bps": 1}, "/i", []))
        with patch.object(worker_mod.runner, "run_capture", return_value=(1, "", "OpenEncodeSessionEx failed")):
            with self.assertRaises(JobFailed) as ctx:
                w._sample_check(job, SETTINGS, probe, {"max_video_bps": 8000, "cq": 25}, "/i", [])
        self.assertEqual(ctx.exception.code, "gpu_busy")
        with patch.object(worker_mod.runner, "run_capture", return_value=(1, "", "other")):
            self.assertIsNone(w._sample_check(job, SETTINGS, probe, {"max_video_bps": 8000, "cq": 25}, "/i", []))

    def test_decode_errors(self):
        w = self.make_worker(FakeClient())
        with patch.object(worker_mod.runner, "run_capture", return_value=(0, "", "")):
            self.assertIsNone(w._decode_errors("/o"))
        with patch.object(worker_mod.runner, "run_capture", return_value=(0, "", "corrupt frame\n")):
            self.assertEqual(w._decode_errors("/o"), "corrupt frame")
        with patch.object(worker_mod.runner, "run_capture", return_value=(1, "", "")):
            self.assertEqual(w._decode_errors("/o"), "decoder exited 1")
        muxer_noise = ("[null @ 0x1] Application provided invalid, non monotonically increasing dts to muxer in stream 0: 2 >= 2\n"
                       "[out#0/null @ 0x2] something about the null output\n")
        with patch.object(worker_mod.runner, "run_capture", return_value=(0, "", muxer_noise)):
            self.assertIsNone(w._decode_errors("/o"))
        with patch.object(worker_mod.runner, "run_capture",
                          return_value=(0, "", muxer_noise + "[hevc @ 0x3] Invalid NAL unit size\n")):
            self.assertEqual(w._decode_errors("/o"), "[hevc @ 0x3] Invalid NAL unit size")


class TestRunLoopAndRecovery(WorkerTestBase):
    def test_paused_idle_drain_and_stop(self):
        self.assertEqual(self.make_worker(FakeClient(pause=True)).run_forever(once=True), "paused")
        self.assertEqual(self.make_worker(FakeClient()).run_forever(once=True), "idle")
        self.assertEqual(self.make_worker(FakeClient(server_version="2.0")).run_forever(once=True), "drain")
        w = self.make_worker(FakeClient())
        w.request_stop()
        self.assertEqual(w.run_forever(), "stopped")

    def test_loops_sleep_when_paused_or_idle(self):
        client = FakeClient(pause=True)
        w = self.make_worker(client)
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            if len(sleeps) == 1:
                client.pause = False
            else:
                w.request_stop()
        w.sleep = sleep
        with self.assertLogs("media_manager.transcode.worker", level="INFO") as logs:
            self.assertEqual(w.run_forever(), "stopped")
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(any("Waiting" in m for m in logs.output))
        self.assertTrue(any("Resuming" in m for m in logs.output))

    def test_waits_without_claiming_while_gpu_unavailable(self):
        # A driver/library mismatch made every claim fail as gpu_busy (free requeue) in a 3 s hot loop.
        client = FakeClient(claims=[{"job": {"id": "j"}, "settings": {}}])
        reasons = ["Failed to initialize NVML: Driver/library version mismatch"]
        w = self.make_worker(client, cfg={"encoder": "hevc_nvenc"},
                             gpu_health=lambda: reasons.pop(0) if reasons else None)
        with self.assertLogs("media_manager.transcode.worker", level="INFO") as logs:
            self.assertEqual(w.run_forever(once=True), "gpu_unavailable")
        self.assertNotIn(("claim",), client.calls)
        self.assertTrue(any("GPU unavailable" in m and "mismatch" in m for m in logs.output))
        with patch.object(w, "process") as process:
            self.assertEqual(w.run_forever(once=True), "done")
        process.assert_called_once()

    def test_heartbeat_reports_gpu_error_and_reboot_timer(self):
        client = FakeClient()
        details = []
        client.heartbeat = lambda detail=None: details.append(detail) or {"pause": False}
        reasons = ["Driver/library version mismatch", None]
        timer = os.path.join(self.tmp, "gpu-reboot.timer")
        open(timer, "w").close()
        w = self.make_worker(client, cfg={"encoder": "hevc_nvenc", "gpu_reboot_timer": timer},
                             gpu_health=lambda: reasons.pop(0))
        w.run_forever(once=True)
        w.run_forever(once=True)
        self.assertEqual(details[1]["gpu_error"], "Driver/library version mismatch")  # first loop beat
        self.assertTrue(details[1]["auto_reboot"])
        self.assertIsNone(details[-1]["gpu_error"])  # cleared as soon as the GPU is back

    def test_gpu_health_ignored_for_software_encoder(self):
        w = self.make_worker(FakeClient(), gpu_health=lambda: "broken")
        self.assertEqual(w.run_forever(once=True), "idle")

    def test_environment_checks(self):
        with self.assertRaises(SystemExit):
            Worker(self.cfg(scratch_dir=os.path.join(self.lib, "scratch")), FakeClient()).check_environment()
        with patch.object(fsops, "can_write_dir", return_value=False), self.assertRaises(SystemExit):
            Worker(self.cfg(), FakeClient()).check_environment()

    def _recovery_job(self, **kw):
        src = os.path.join(self.lib, "Movie", "M.mp4")
        out = os.path.join(self.lib, "Movie", "M.mkv")
        base = {"id": "rec12345", "title": "M", "media_type": "movie", "source_path": src, "source_size": 3,
                "phase": "swapping", "output_path": out, "temp_path": out + ".tmp", "backup_path": fsops.backup_path_for(src)}
        base.update(kw)
        return base

    def _write(self, path, data=b"new"):
        with open(path, "wb") as fh:
            fh.write(data)

    def test_recover_cleanup_retry_rollback_complete_manual(self):
        # pre-swap: temp removed, job requeued, original untouched
        job = self._recovery_job(phase="encoding")
        self._write(job["source_path"], b"old")
        self._write(job["temp_path"])
        client = FakeClient(inflight=[job])
        self.make_worker(client).recover_inflight()
        self.assertFalse(os.path.exists(job["temp_path"]))
        self.assertEqual(client.failures()[0][0], "worker_lost")

        # swapped but hash mismatch: roll back to the original
        os.rename(job["source_path"], job["backup_path"])
        self._write(job["output_path"], b"corrupt")
        client = FakeClient(inflight=[dict(job, phase="swapped", output_hash="deadbeef")])
        self.make_worker(client).recover_inflight()
        self.assertTrue(os.path.exists(job["source_path"]))
        self.assertFalse(os.path.exists(job["output_path"]))
        self.assertIn("restored", client.failures()[0][1])

        # swapped with matching hash: report completion
        os.rename(job["source_path"], job["backup_path"])
        self._write(job["output_path"], b"good")
        client = FakeClient(inflight=[dict(job, phase="swapped", output_hash=fsops.hash_file(job["output_path"]))])
        w = self.make_worker(client)
        w.settings_by_type["movie"] = dict(SETTINGS)
        with patch.object(worker_mod.runner, "probe", side_effect=RuntimeError("no ffprobe")):
            w.recover_inflight()
        self.assertEqual(client.completions()[0]["output_path"], job["output_path"])
        self.assertIsNone(client.completions()[0]["probe"])

        # nonsense on disk: flag for a human, touch nothing
        client = FakeClient(inflight=[self._recovery_job(phase="encoding", source_path="/nope/x.mp4")])
        self.make_worker(client).recover_inflight()
        self.assertEqual(client.failures()[0][0], "needs_manual_review")

    def test_recover_rolls_back_from_filebot_renamed_backup(self):
        # Pre-hidden backup name recorded; FileBot renamed "M.mp4.mm-orig" -> "M.mm-orig".
        job = self._recovery_job(backup_path=self._recovery_job()["source_path"] + ".mm-orig")
        renamed = os.path.join(self.lib, "Movie", "M.mm-orig")
        self._write(renamed, b"old")
        self._write(job["output_path"], b"corrupt")
        client = FakeClient(inflight=[dict(job, phase="swapped", output_hash="deadbeef")])
        self.make_worker(client).recover_inflight()
        self.assertFalse(os.path.exists(renamed))
        with open(job["source_path"], "rb") as fh:
            self.assertEqual(fh.read(), b"old")
        self.assertIn("restored", client.failures()[0][1])

    def _assert_recover_refused(self, job, keep):
        client = FakeClient(inflight=[job])
        with patch.object(worker_mod.os, "rename") as rename, \
                patch.object(worker_mod.fsops, "remove_quietly") as remove:
            self.make_worker(client).recover_inflight()
        rename.assert_not_called()
        remove.assert_not_called()
        for path in keep:
            self.assertTrue(os.path.exists(path), path)
        self.assertEqual(len(client.failures()), 1)
        code, message = client.failures()[0]
        self.assertEqual(code, "needs_manual_review")
        self.assertIn("path outside allowed roots", message)

    def test_recover_refuses_paths_outside_allowed_roots(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        victim = os.path.join(outside, "victim.mkv")
        self._write(victim, b"precious")
        good = self._recovery_job()
        cases = {
            "source": dict(source_path=victim, phase="swapped", output_hash="deadbeef"),
            "output": dict(output_path=victim, phase="swapped", output_hash="deadbeef"),
            "temp": dict(temp_path=victim, phase="encoding"),
            "backup": dict(backup_path=victim, phase="swapped", output_hash="deadbeef"),
            "dotdot": dict(temp_path=os.path.join(self.lib, "Movie", "..", "..", "outside", "victim.mkv"),
                           phase="encoding"),
            "etc": dict(temp_path="/etc/passwd", phase="encoding"),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                self._write(good["source_path"], b"old")
                self._write(good["output_path"], b"new")
                self._assert_recover_refused(self._recovery_job(**kw), [victim, good["source_path"]])

    def test_recover_refuses_temp_or_backup_in_another_folder(self):
        other = os.path.join(self.lib, "Other")
        os.makedirs(other)
        stray = os.path.join(other, "stray.mkv")
        self._write(stray, b"keep")
        for kw in (dict(temp_path=stray, phase="encoding"),
                   dict(backup_path=stray, phase="swapped", output_hash="deadbeef")):
            with self.subTest(kw):
                self._write(self._recovery_job()["output_path"], b"new")
                self._assert_recover_refused(self._recovery_job(**kw), [stray])

    def test_recover_refuses_symlink_escape(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        victim = os.path.join(outside, "victim.mkv")
        self._write(victim, b"precious")
        link = os.path.join(self.lib, "Movie", ".M.rec12345.mm-partial")
        os.symlink(victim, link)
        self._assert_recover_refused(self._recovery_job(temp_path=link, phase="encoding"), [victim])

    def test_recover_allows_source_dirs(self):
        dl = os.path.join(self.tmp, "downloads")
        os.makedirs(dl)
        src = os.path.join(dl, "M.mp4")
        job = self._recovery_job(source_path=src, output_path=os.path.join(dl, "M.mkv"),
                                 temp_path=os.path.join(dl, ".M.rec12345.mm-partial"),
                                 backup_path=fsops.backup_path_for(src), phase="encoding")
        self._write(src, b"old")
        self._write(job["temp_path"])
        client = FakeClient(inflight=[job])
        self.make_worker(client, cfg={"source_dirs": [dl]}).recover_inflight()
        self.assertFalse(os.path.exists(job["temp_path"]))
        self.assertEqual(client.failures()[0][0], "worker_lost")

    def test_recovery_exception_is_reported(self):
        client = FakeClient(inflight=[{"id": "bad"}])  # missing keys -> KeyError
        self.make_worker(client).recover_inflight()
        self.assertEqual(client.failures()[0][0], "needs_manual_review")


class TestConfigAndMain(unittest.TestCase):
    def test_from_env(self):
        with self.assertRaises(SystemExit):
            WorkerConfig.from_env({})
        cfg = WorkerConfig.from_env({"TRANSCODE_API_URL": "https://a", "TRANSCODE_WORKER_TOKEN": "t",
                                     "TRANSCODE_SCRATCH_DIR": "/s", "TRANSCODE_TLS_VERIFY": "0",
                                     "TRANSCODE_SAMPLE_CHECK": "no", "TRANSCODE_GPU_INDEX": "1"})
        self.assertEqual((cfg.api_url, cfg.gpu_index, cfg.tls_verify, cfg.sample_check), ("https://a", 1, False, False))
        self.assertTrue(cfg.worker_id)
        self.assertTrue(cfg.media_dirs)

    def test_cli_command_skips_db_setup(self):
        from click.testing import CliRunner
        import main
        with patch("media_manager.transcode.worker.main", return_value=0) as wmain, \
             patch.object(main, "init_db_schema_sync") as init_db, patch.object(main, "setup_logging") as logs:
            result = CliRunner().invoke(main.cli, ["transcode-worker", "--once"])
        self.assertEqual(result.exit_code, 0)
        wmain.assert_called_once_with(["--once"])
        init_db.assert_not_called()
        logs.assert_not_called()

    def test_main_wires_signals_and_runs(self):
        env = {"TRANSCODE_API_URL": "https://a", "TRANSCODE_WORKER_TOKEN": "t", "TRANSCODE_SCRATCH_DIR": "/s"}
        with patch.dict(os.environ, env), patch.object(worker_mod, "Worker") as W, \
             patch.object(worker_mod.signal, "signal") as sig:
            W.return_value.run_forever.return_value = "idle"
            self.assertEqual(worker_mod.main(["--once"]), 0)
        W.return_value.run_forever.assert_called_once_with(once=True)
        self.assertEqual(sig.call_count, 2)


if __name__ == "__main__":
    unittest.main()
