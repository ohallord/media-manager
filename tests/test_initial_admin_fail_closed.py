"""The bootstrap admin must never fall back to a hardcoded password.

If INITIAL_ADMIN_PASSWORD is unset, first-login bootstrap is disabled (fail
closed). When it is set, the bootstrapped admin must change it on first login.
"""
import asyncio
import importlib
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from media_manager.database import AsyncSessionLocal
from media_manager.models import User


_LOOP = asyncio.new_event_loop()


def _run(coro):
    # Own loop: asyncio.run() would clear the default loop other tests use.
    return _LOOP.run_until_complete(coro)


async def _clear_users():
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User))
        await db.commit()


async def _get_user(username):
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.username == username))
        return res.scalar_one_or_none()


class TestConfigHasNoDefaultPassword(unittest.TestCase):
    def test_unset_env_gives_no_password(self):
        import media_manager.config as config
        env = {k: v for k, v in os.environ.items() if k != "INITIAL_ADMIN_PASSWORD"}
        with mock.patch.dict(os.environ, env, clear=True):
            reloaded = importlib.reload(config)
            self.assertFalse(reloaded.INITIAL_ADMIN_PASSWORD)
        importlib.reload(config)

    def test_empty_env_gives_no_password(self):
        import media_manager.config as config
        with mock.patch.dict(os.environ, {"INITIAL_ADMIN_PASSWORD": ""}):
            reloaded = importlib.reload(config)
            self.assertFalse(reloaded.INITIAL_ADMIN_PASSWORD)
        importlib.reload(config)


class TestBootstrapLogin(unittest.TestCase):
    def setUp(self):
        from media_manager.web.app import app
        self.client = TestClient(app)
        _run(_clear_users())

    def tearDown(self):
        _run(_clear_users())

    def test_unset_password_refuses_bootstrap(self):
        with mock.patch("media_manager.web.api.auth.INITIAL_ADMIN_PASSWORD", None):
            for pw in ("", "admin", "changeme"):
                r = self.client.post("/api/v1/auth/login", json={"username": "admin", "password": pw})
                self.assertEqual(r.status_code, 401, pw)
        self.assertIsNone(_run(_get_user("admin")))

    def test_set_password_bootstraps_and_forces_reset(self):
        with mock.patch("media_manager.web.api.auth.INITIAL_ADMIN_PASSWORD", "S3t-by-env-only"):
            r = self.client.post("/api/v1/auth/login", json={"username": "admin", "password": "S3t-by-env-only"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["user"]["must_change_password"])
        user = _run(_get_user("admin"))
        self.assertIsNotNone(user)
        self.assertTrue(user.must_change_password)


if __name__ == "__main__":
    unittest.main()
