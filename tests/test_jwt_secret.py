"""Security review 2026-11-07 MEDIUM 4: JWT_SECRET must never fall back to a public default."""
import os
import stat
import tempfile
import unittest

from media_manager import config

LEGACY = "dev-secret-key-change-in-production"


class TestJwtSecretResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.secret_file = os.path.join(self.tmp.name, ".jwt_secret")

    def tearDown(self):
        self.tmp.cleanup()

    def test_loaded_secret_is_not_the_public_default(self):
        self.assertNotEqual(config.JWT_SECRET, LEGACY)
        self.assertGreaterEqual(len(config.JWT_SECRET), 32)

    def test_explicit_env_value_wins(self):
        self.assertEqual(config._resolve_jwt_secret("a-real-secret", self.secret_file), "a-real-secret")
        self.assertFalse(os.path.exists(self.secret_file))

    def test_legacy_default_is_replaced_and_persisted(self):
        first = config._resolve_jwt_secret(LEGACY, self.secret_file)
        self.assertNotEqual(first, LEGACY)
        self.assertGreaterEqual(len(first), 32)
        self.assertEqual(stat.S_IMODE(os.stat(self.secret_file).st_mode), 0o600)
        # Stable across restarts: the persisted value is reused.
        self.assertEqual(config._resolve_jwt_secret(None, self.secret_file), first)

    def test_unset_and_unwritable_falls_back_to_ephemeral_random(self):
        bad = os.path.join(self.tmp.name, "missing-dir", ".jwt_secret")
        a = config._resolve_jwt_secret("", bad)
        b = config._resolve_jwt_secret("", bad)
        self.assertGreaterEqual(len(a), 32)
        self.assertNotEqual(a, b)

    def test_no_secret_file_location_gives_ephemeral_random(self):
        self.assertGreaterEqual(len(config._resolve_jwt_secret(None, None)), 32)

    def test_empty_persisted_file_is_regenerated(self):
        open(self.secret_file, "w").close()
        self.assertGreaterEqual(len(config._resolve_jwt_secret(None, self.secret_file)), 32)


if __name__ == "__main__":
    unittest.main()
