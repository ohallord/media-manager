import os
import shutil
import stat
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "fix_permissions.sh")


class TestFixPermissionsScript(unittest.TestCase):
    def setUp(self):
        self.lib = tempfile.mkdtemp(prefix="mm_perm_")
        self.sub = os.path.join(self.lib, "Show", "Season 1")
        os.makedirs(self.sub)
        self.file = os.path.join(self.sub, "1x01.mkv")
        with open(self.file, "w") as fh:
            fh.write("x")
        # Explicit modes (not umask-dependent): 3 entries lack bits - root, season folder, file.
        os.chmod(self.lib, 0o700)
        os.chmod(os.path.dirname(self.sub), 0o775)
        os.chmod(self.sub, 0o700)
        os.chmod(self.file, 0o600)
        self.owner = f"{os.getuid()}:{os.getgid()}"

    def tearDown(self):
        shutil.rmtree(self.lib, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run(["bash", SCRIPT, "--owner", self.owner, *args],
                              capture_output=True, text=True, timeout=60)

    def test_dry_run_reports_without_changing(self):
        out = self._run(self.lib)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("0 with wrong owner, 3 missing", out.stdout)  # temp root, season folder, file
        self.assertIn("Re-run with", out.stdout)
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)

    def test_apply_only_adds_bits(self):
        os.chmod(self.file, 0o700)  # executable bit must survive
        out = self._run("--apply", self.lib)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o764)
        self.assertEqual(stat.S_IMODE(os.stat(self.sub).st_mode), 0o775)
        self.assertIn("Done:", out.stdout)
        self.assertIn("0 missing", self._run(self.lib).stdout)

    def test_skips_missing_and_empty_dirs_and_validates_args(self):
        empty = tempfile.mkdtemp(prefix="mm_perm_empty_")
        try:
            out = self._run("/no/such/dir", empty)
            self.assertIn("not a directory", out.stdout)
            self.assertIn("empty - mount may be down", out.stdout)
        finally:
            os.rmdir(empty)
        self.assertEqual(subprocess.run(["bash", SCRIPT, "--owner", "alice"], capture_output=True).returncode, 2)
        self.assertEqual(subprocess.run(["bash", SCRIPT, "--bogus"], capture_output=True).returncode, 2)
        self.assertIn("Usage", subprocess.run(["bash", SCRIPT, "--help"], capture_output=True, text=True).stdout)

    def test_apply_as_other_user_needs_root(self):
        if os.getuid() == 0:
            self.skipTest("running as root")
        out = subprocess.run(["bash", SCRIPT, "--apply", "--owner", "54321:54321", self.lib],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 1)
        self.assertIn("needs root", out.stderr)


if __name__ == "__main__":
    unittest.main()
