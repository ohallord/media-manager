import json
import sys
import time
import unittest
from types import SimpleNamespace

from media_manager.transcode import runner

FAKE_FFMPEG = r"""
import sys, time
for i in range(3):
    print(f"out_time_us={i * 1000000}"); print("fps=100"); print("bogus line")
    print("progress=continue" if i < 2 else "progress=end"); sys.stdout.flush()
sys.stderr.write("warning one\n\nwarning two\n")
sys.exit(int(sys.argv[1]))
"""


class TestFfmpegProcess(unittest.TestCase):
    def test_progress_blocks_and_stderr(self):
        blocks = []
        proc = runner.FfmpegProcess([sys.executable, "-c", FAKE_FFMPEG, "3"], on_progress=blocks.append).start()
        self.assertEqual(proc.wait(timeout=30), 3)
        self.assertEqual([b["out_time_us"] for b in blocks], ["0", "1000000", "2000000"])
        self.assertEqual(blocks[-1]["progress"], "end")
        self.assertEqual(proc.errors, "warning one\nwarning two")

    def test_progress_callback_errors_are_swallowed(self):
        def bad(_):
            raise RuntimeError("reporting failed")
        proc = runner.FfmpegProcess([sys.executable, "-c", FAKE_FFMPEG, "0"], on_progress=bad).start()
        self.assertEqual(proc.wait(timeout=30), 0)

    def test_pause_resume_terminate(self):
        proc = runner.FfmpegProcess([sys.executable, "-c", "import time; time.sleep(60)"]).start()
        self.assertIsNone(proc.poll())
        proc.pause()
        self.assertTrue(proc.paused)
        proc.pause()  # idempotent
        proc.resume()
        self.assertFalse(proc.paused)
        proc.pause()
        proc.terminate(grace=5)  # resumes first so SIGTERM is handled
        self.assertIsNotNone(proc.poll())
        proc.terminate()  # already exited: no-op
        proc.resume()

    def test_terminate_kills_after_grace(self):
        script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
        proc = runner.FfmpegProcess([sys.executable, "-c", script]).start()
        time.sleep(0.5)
        proc.terminate(grace=0.5)
        self.assertIsNotNone(proc.poll())


class TestProbe(unittest.TestCase):
    def test_probe_and_capture(self):
        seen = []

        def run(args, **kw):
            seen.append(args)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": []}), stderr="")
        self.assertEqual(runner.probe("/a.mkv", "ffprobe", count_packets=True, run=run), {"streams": []})
        self.assertIn("-count_packets", seen[0])
        self.assertEqual(seen[0][-1], "/a.mkv")

        def fail(args, **kw):
            return SimpleNamespace(returncode=1, stdout="", stderr="Invalid data")
        with self.assertRaises(RuntimeError):
            runner.probe("/a.mkv", run=fail)


if __name__ == "__main__":
    unittest.main()
