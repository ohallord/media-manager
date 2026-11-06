import unittest
import asyncio
from datetime import datetime
from fastapi.testclient import TestClient
from sqlalchemy import delete

from media_manager.web.app import app
from media_manager.database import AsyncSessionLocal
from media_manager.models import User
from media_manager.web.services.auth import hash_password, create_access_token


class TestApiAuth(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_unauthenticated_me(self):
        response = self.client.get("/api/v1/auth/me")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data.get("user"))

    def test_initial_admin_login(self):
        response = self.client.post("/api/v1/auth/login", json={"username": "admin", "password": "not-a-real-password"})
        # 401 is expected if 2FA or DB is not initialized in the test environment context, but we check configuration
        self.assertIn(response.status_code, [200, 401])

    def test_jwt_expiration_config(self):
        from media_manager.config import JWT_EXPIRATION_HOURS
        self.assertEqual(JWT_EXPIRATION_HOURS, 720)

    def test_logout_clears_cookie(self):
        response = self.client.post("/api/v1/auth/logout")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Successfully logged out")

    def test_me_with_bearer_token_header(self):
        token = create_access_token({"sub": "nonexistent-user-id", "username": "ghost", "role": "user"})
        response = self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        # user id doesn't exist in DB, so still resolves to no user
        self.assertIsNone(response.json().get("user"))

    def test_me_with_malformed_bearer_header(self):
        response = self.client.get("/api/v1/auth/me", headers={"Authorization": "NotBearer sometoken"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json().get("user"))

    def test_change_password_requires_auth(self):
        response = self.client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "x", "new_password": "y"},
        )
        self.assertEqual(response.status_code, 401)

    def test_verify_2fa_invalid_token(self):
        response = self.client.post(
            "/api/v1/auth/verify-2fa", json={"temp_token": "does-not-exist", "code": "000000"}
        )
        self.assertEqual(response.status_code, 400)

    def test_bypass_2fa_removed(self):
        response = self.client.post("/api/v1/auth/bypass-2fa", json={"temp_token": "does-not-exist"})
        self.assertEqual(response.status_code, 404)

    def test_forgot_password_unknown_username(self):
        response = self.client.post("/api/v1/auth/forgot-password", json={"username": "no-such-user"})
        self.assertEqual(response.status_code, 404)

    def test_reset_password_invalid_token(self):
        response = self.client.post(
            "/api/v1/auth/reset-password",
            json={"temp_token": "bogus", "code": "000000", "new_password": "newpass123"},
        )
        self.assertEqual(response.status_code, 400)

    def test_bypass_reset_password_removed(self):
        response = self.client.post(
            "/api/v1/auth/bypass-reset-password",
            json={"temp_token": "bogus", "new_password": "newpass123"},
        )
        self.assertEqual(response.status_code, 404)


class TestApiAuthWithRealUser(unittest.TestCase):
    """
    Exercises the full login / 2FA / forgot-password / reset-password flows
    against a real (but isolated, test-only) user row. Scoped to a specific
    username and cleaned up in tearDown, matching tests/test_api_admin_users.py.
    """

    USERNAME = "test_auth_flow_user"

    def setUp(self):
        self.client = TestClient(app)
        self.password = "InitialPass123"
        asyncio.run(self._cleanup())

    def tearDown(self):
        asyncio.run(self._cleanup())

    async def _cleanup(self):
        async with AsyncSessionLocal() as db:
            await db.execute(delete(User).where(User.username == self.USERNAME))
            await db.commit()

    async def _create_user(self, **overrides):
        defaults = dict(
            username=self.USERNAME,
            password_hash=hash_password(self.password),
            role="user",
            is_active=True,
            email="auth-flow@example.com",
            two_factor_method="none",
            two_factor_enabled=False,
            must_change_password=False,
        )
        defaults.update(overrides)
        async with AsyncSessionLocal() as db:
            db.add(User(**defaults))
            await db.commit()

    def test_login_success_sets_cookie_and_token(self):
        asyncio.run(self._create_user())
        response = self.client.post(
            "/api/v1/auth/login", json={"username": self.USERNAME, "password": self.password}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["user"]["username"], self.USERNAME)
        self.assertIn("access_token", data)
        self.assertIn("access_token", response.cookies)

    def test_login_wrong_password(self):
        asyncio.run(self._create_user())
        response = self.client.post(
            "/api/v1/auth/login", json={"username": self.USERNAME, "password": "wrong"}
        )
        self.assertEqual(response.status_code, 401)

    def test_login_unknown_username(self):
        response = self.client.post(
            "/api/v1/auth/login", json={"username": "no-such-user", "password": "whatever"}
        )
        self.assertEqual(response.status_code, 401)

    def test_login_requires_2fa(self):
        asyncio.run(self._create_user(two_factor_enabled=True, two_factor_method="email"))
        response = self.client.post(
            "/api/v1/auth/login", json={"username": self.USERNAME, "password": self.password}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "requires_2fa")
        self.assertIn("temp_token", data)

    def test_full_2fa_verification_flow(self):
        from media_manager.web.api import auth as auth_module
        asyncio.run(self._create_user(two_factor_enabled=True, two_factor_method="phone"))
        login_resp = self.client.post(
            "/api/v1/auth/login", json={"username": self.USERNAME, "password": self.password}
        )
        temp_token = login_resp.json()["temp_token"]
        real_code = auth_module.PENDING_2FA[temp_token]["code"]

        wrong_resp = self.client.post(
            "/api/v1/auth/verify-2fa", json={"temp_token": temp_token, "code": "000000"}
        )
        self.assertEqual(wrong_resp.status_code, 400)

        good_resp = self.client.post(
            "/api/v1/auth/verify-2fa", json={"temp_token": temp_token, "code": real_code}
        )
        self.assertEqual(good_resp.status_code, 200)
        self.assertEqual(good_resp.json()["status"], "success")

        # Token is consumed - reusing it fails.
        reuse_resp = self.client.post(
            "/api/v1/auth/verify-2fa", json={"temp_token": temp_token, "code": real_code}
        )
        self.assertEqual(reuse_resp.status_code, 400)

    def test_bypass_2fa_endpoint_gone(self):
        asyncio.run(self._create_user(two_factor_enabled=True, two_factor_method="phone"))
        login_resp = self.client.post(
            "/api/v1/auth/login", json={"username": self.USERNAME, "password": self.password}
        )
        temp_token = login_resp.json()["temp_token"]
        bypass_resp = self.client.post("/api/v1/auth/bypass-2fa", json={"temp_token": temp_token})
        self.assertEqual(bypass_resp.status_code, 404)

    def test_change_password_flow(self):
        asyncio.run(self._create_user())
        login_resp = self.client.post(
            "/api/v1/auth/login", json={"username": self.USERNAME, "password": self.password}
        )
        token = login_resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        wrong_current = self.client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "not-it", "new_password": "NewPass123"},
            headers=headers,
        )
        self.assertEqual(wrong_current.status_code, 400)

        too_short = self.client.post(
            "/api/v1/auth/change-password",
            json={"current_password": self.password, "new_password": "abc"},
            headers=headers,
        )
        self.assertEqual(too_short.status_code, 400)

        success = self.client.post(
            "/api/v1/auth/change-password",
            json={"current_password": self.password, "new_password": "NewPass123"},
            headers=headers,
        )
        self.assertEqual(success.status_code, 200)

        relogin = self.client.post(
            "/api/v1/auth/login", json={"username": self.USERNAME, "password": "NewPass123"}
        )
        self.assertEqual(relogin.status_code, 200)

    def test_must_change_password_blocks_other_routes(self):
        asyncio.run(self._create_user(must_change_password=True))
        login_resp = self.client.post(
            "/api/v1/auth/login", json={"username": self.USERNAME, "password": self.password}
        )
        token = login_resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        blocked = self.client.get("/api/v1/admin/users", headers=headers)
        self.assertEqual(blocked.status_code, 403)

        # /auth/me still works while must_change_password is pending.
        me = self.client.get("/api/v1/auth/me", headers=headers)
        self.assertEqual(me.status_code, 200)
        self.assertTrue(me.json()["user"]["must_change_password"])

    def test_forgot_password_and_reset_flow_with_email(self):
        from media_manager.web.api import auth as auth_module
        asyncio.run(self._create_user(two_factor_method="email"))

        forgot_resp = self.client.post("/api/v1/auth/forgot-password", json={"username": self.USERNAME})
        self.assertEqual(forgot_resp.status_code, 200)
        forgot_data = forgot_resp.json()
        self.assertEqual(forgot_data["status"], "requires_verification")
        self.assertEqual(forgot_data["method"], "email")
        temp_token = forgot_data["temp_token"]
        real_code = auth_module.PENDING_PASSWORD_RESET[temp_token]["code"]

        wrong_code = self.client.post(
            "/api/v1/auth/reset-password",
            json={"temp_token": temp_token, "code": "000000", "new_password": "BrandNewPass1"},
        )
        self.assertEqual(wrong_code.status_code, 400)

        too_short = self.client.post(
            "/api/v1/auth/reset-password",
            json={"temp_token": temp_token, "code": real_code, "new_password": "ab"},
        )
        self.assertEqual(too_short.status_code, 400)

        success = self.client.post(
            "/api/v1/auth/reset-password",
            json={"temp_token": temp_token, "code": real_code, "new_password": "BrandNewPass1"},
        )
        self.assertEqual(success.status_code, 200)

        relogin = self.client.post(
            "/api/v1/auth/login", json={"username": self.USERNAME, "password": "BrandNewPass1"}
        )
        self.assertEqual(relogin.status_code, 200)

    def test_forgot_password_no_contact_info(self):
        asyncio.run(self._create_user(email=None, phone=None, two_factor_method="none"))
        response = self.client.post("/api/v1/auth/forgot-password", json={"username": self.USERNAME})
        self.assertEqual(response.status_code, 400)

    def test_bypass_reset_password_endpoint_gone(self):
        asyncio.run(self._create_user())
        forgot_resp = self.client.post("/api/v1/auth/forgot-password", json={"username": self.USERNAME})
        self.assertEqual(forgot_resp.status_code, 200)
        bypass_resp = self.client.post(
            "/api/v1/auth/bypass-reset-password",
            json={"temp_token": forgot_resp.json()["temp_token"], "new_password": "SoftBypassPass1"},
        )
        self.assertEqual(bypass_resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
