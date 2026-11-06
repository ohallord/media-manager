import subprocess
import unittest
from types import SimpleNamespace

from media_manager.transcode import gpu


class TestGpu(unittest.TestCase):
    def test_parse_query(self):
        self.assertEqual(gpu.parse_query("2, 35, 10, 9000\n"),
                         {"encoder_sessions": 2, "encoder_util": 35, "decoder_util": 10, "mem_free_mb": 9000})
        self.assertIsNone(gpu.parse_query(""))
        self.assertIsNone(gpu.parse_query("[N/A], 1, 2, 3"))

    def test_query(self):
        calls = []

        def run(args, **kw):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout="1, 50, 0, 8000")
        self.assertEqual(gpu.query("nvidia-smi", 1, run=run)["encoder_sessions"], 1)
        self.assertIn("--id=1", calls[0])
        self.assertIsNone(gpu.query(run=lambda *a, **k: SimpleNamespace(returncode=9, stdout="")))

        def boom(*a, **k):
            raise FileNotFoundError("no nvidia-smi")
        self.assertIsNone(gpu.query(run=boom))

        def slow(*a, **k):
            raise subprocess.TimeoutExpired("nvidia-smi", 15)
        self.assertIsNone(gpu.query(run=slow))

    def test_unavailable(self):
        ok = lambda *a, **k: SimpleNamespace(returncode=0, stdout="1, 50, 0, 8000", stderr="")
        self.assertIsNone(gpu.unavailable(run=ok))
        bad = lambda *a, **k: SimpleNamespace(returncode=18, stdout="Failed to initialize NVML: Driver/library "
                                              "version mismatch\nNVML library version: 595.99\n", stderr="")
        self.assertEqual(gpu.unavailable(run=bad), "Failed to initialize NVML: Driver/library version mismatch")
        quiet = lambda *a, **k: SimpleNamespace(returncode=9, stdout="", stderr="")
        self.assertEqual(gpu.unavailable(run=quiet), "nvidia-smi exited 9")

        def missing(*a, **k):
            raise FileNotFoundError("no nvidia-smi")
        self.assertIsNone(gpu.unavailable(run=missing))  # can't tell: let ffmpeg decide

        def slow(*a, **k):
            raise subprocess.TimeoutExpired("nvidia-smi", 15)
        self.assertIn("timed out", gpu.unavailable(run=slow))

    def test_contention(self):
        info = lambda s, enc=50, mem=8000: {"encoder_sessions": s, "encoder_util": enc, "decoder_util": 0, "mem_free_mb": mem}
        self.assertIsNone(gpu.contention(None, 1))
        self.assertIsNone(gpu.contention(info(1), 1))  # only us
        self.assertIn("other encode", gpu.contention(info(2), 1))
        self.assertIsNone(gpu.contention(info(2, enc=10), 1))  # another session but idle
        self.assertIn("other encode", gpu.contention(info(1, enc=5), 0))  # we're paused, someone else encodes
        self.assertIn("low VRAM", gpu.contention(info(1, mem=500), 1))


if __name__ == "__main__":
    unittest.main()
