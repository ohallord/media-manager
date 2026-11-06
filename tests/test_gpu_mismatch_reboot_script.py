import os
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "gpu_mismatch_reboot.sh")


class TestGpuMismatchRebootScript(unittest.TestCase):
    def _run(self, smi_output, smi_rc):
        with tempfile.TemporaryDirectory() as tmp:
            smi = os.path.join(tmp, "nvidia-smi")
            with open(smi, "w") as fh:
                fh.write(f"#!/bin/bash\necho '{smi_output}'\nexit {smi_rc}\n")
            os.chmod(smi, 0o755)
            marker = os.path.join(tmp, "rebooted")
            env = dict(os.environ, NVIDIA_SMI=smi, REBOOT_CMD=f"touch {marker}")
            res = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True)
            return res.returncode, os.path.exists(marker)

    def test_reboots_only_on_version_mismatch(self):
        self.assertEqual(self._run("Failed to initialize NVML: Driver/library version mismatch", 18), (0, True))
        self.assertEqual(self._run("NVIDIA-SMI has failed because it couldn't communicate with the driver", 9),
                         (0, False))
        self.assertEqual(self._run("| GPU  Name |", 0), (0, False))


if __name__ == "__main__":
    unittest.main()
