import unittest
from datetime import timedelta
from unittest.mock import patch, MagicMock

from media_manager.web.services.auth import (
    hash_password, verify_password, create_access_token, decode_access_token,
    get_webauthn_reg_options, get_webauthn_auth_options, webauthn_challenges
)


class TestPasswordHashing(unittest.TestCase):
    def test_hash_and_verify_roundtrip(self):
        h = hash_password("correct-horse-battery-staple")
        self.assertTrue(verify_password(h, "correct-horse-battery-staple"))

    def test_verify_wrong_password(self):
        h = hash_password("correct-horse-battery-staple")
        self.assertFalse(verify_password(h, "wrong-password"))

    def test_verify_malformed_hash(self):
        self.assertFalse(verify_password("not-a-real-hash", "anything"))


class TestAccessToken(unittest.TestCase):
    def test_create_and_decode_roundtrip(self):
        token = create_access_token({"sub": "user-123", "username": "alice", "role": "admin"})
        payload = decode_access_token(token)
        self.assertEqual(payload["sub"], "user-123")
        self.assertEqual(payload["username"], "alice")
        self.assertEqual(payload["role"], "admin")
        self.assertIn("exp", payload)

    def test_create_with_explicit_expiry(self):
        token = create_access_token({"sub": "user-123"}, expires_delta=timedelta(minutes=5))
        payload = decode_access_token(token)
        self.assertEqual(payload["sub"], "user-123")

    def test_decode_invalid_token(self):
        self.assertIsNone(decode_access_token("not-a-jwt"))

    def test_decode_garbage_token(self):
        self.assertIsNone(decode_access_token("a.b.c"))


class TestWebauthnOptions(unittest.TestCase):
    def tearDown(self):
        webauthn_challenges.clear()

    def test_get_webauthn_reg_options_stores_challenge(self):
        options = get_webauthn_reg_options("user-1", "alice")
        self.assertIn("user-1", webauthn_challenges)
        self.assertEqual(webauthn_challenges["user-1"], options.challenge)

    def test_get_webauthn_auth_options_stores_challenge(self):
        options = get_webauthn_auth_options("user-1")
        self.assertIn("user-1", webauthn_challenges)
        self.assertEqual(webauthn_challenges["user-1"], options.challenge)


if __name__ == "__main__":
    unittest.main()
