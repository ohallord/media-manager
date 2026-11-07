import unittest
import asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, delete

from media_manager.web.app import app
from media_manager.database import AsyncSessionLocal
from media_manager.models import User
from media_manager.web.api.auth import get_current_active_user, get_admin_user

class TestApiAdminUsers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # We need to run async database commands.
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)
        
    def setUp(self):
        self.client = TestClient(app)
        self.db = AsyncSessionLocal()
        
        # Create test users
        self.admin_user = User(
            id="test-admin-id",
            username="test_admin_user",
            first_name="Admin",
            last_name="Test",
            email="admin@test.com",
            phone="11111",
            role="admin",
            two_factor_method="none",
            two_factor_enabled=False,
            is_active=True,
            must_change_password=False
        )
        self.regular_user = User(
            id="test-regular-id",
            username="test_regular_user",
            first_name="Regular",
            last_name="Test",
            email="regular@test.com",
            phone="22222",
            role="user",
            two_factor_method="none",
            two_factor_enabled=False,
            is_active=True,
            must_change_password=False
        )

        async def _setup():
            async with self.db as session:
                # Clean up any remnants
                await session.execute(delete(User).where(User.username.in_(["test_admin_user", "test_regular_user"])))
                session.add(self.admin_user)
                session.add(self.regular_user)
                await session.commit()

        self.loop.run_until_complete(_setup())

    def tearDown(self):
        async def _teardown():
            async with self.db as session:
                await session.execute(delete(User).where(User.username.in_(["test_admin_user", "test_regular_user"])))
                await session.commit()
        self.loop.run_until_complete(_teardown())
        app.dependency_overrides.clear()

    def test_list_users_as_admin(self):
        # Admin gets all users
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.get("/api/v1/admin/users")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        usernames = [u["username"] for u in data["users"]]
        self.assertIn("test_admin_user", usernames)
        self.assertIn("test_regular_user", usernames)

    def test_list_users_as_regular_user(self):
        # Regular user only gets themselves
        app.dependency_overrides[get_current_active_user] = lambda: self.regular_user
        response = self.client.get("/api/v1/admin/users")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["users"]), 1)
        self.assertEqual(data["users"][0]["username"], "test_regular_user")

    def test_edit_user_as_admin(self):
        # Admin edits regular user's details and role
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        payload = {
            "first_name": "UpdatedRegular",
            "last_name": "UpdatedTest",
            "email": "updated@test.com",
            "phone": "33333",
            "role": "admin",
            "two_factor_method": "none"
        }
        response = self.client.put(f"/api/v1/admin/users/{self.regular_user.id}", json=payload)
        self.assertEqual(response.status_code, 200)
        
        # Verify changes in db
        async def verify():
            async with AsyncSessionLocal() as session:
                res = await session.execute(select(User).where(User.id == self.regular_user.id))
                u = res.scalar_one()
                self.assertEqual(u.first_name, "UpdatedRegular")
                self.assertEqual(u.role, "admin")
        self.loop.run_until_complete(verify())

    def test_edit_user_as_themselves(self):
        # Regular user edits their own details
        app.dependency_overrides[get_current_active_user] = lambda: self.regular_user
        payload = {
            "first_name": "SelfEdit",
            "last_name": "SelfEditLast",
            "email": "self@test.com",
            "phone": "44444",
            "role": "admin", # Attempting to escalate role to admin
            "two_factor_method": "none"
        }
        response = self.client.put(f"/api/v1/admin/users/{self.regular_user.id}", json=payload)
        self.assertEqual(response.status_code, 200)
        
        # Verify changes in db: details updated, but role remains 'user'
        async def verify():
            async with AsyncSessionLocal() as session:
                res = await session.execute(select(User).where(User.id == self.regular_user.id))
                u = res.scalar_one()
                self.assertEqual(u.first_name, "SelfEdit")
                self.assertEqual(u.role, "user") # Not escalated!
        self.loop.run_until_complete(verify())

    def _self_edit(self, **overrides):
        app.dependency_overrides[get_current_active_user] = lambda: self.regular_user
        payload = {
            "first_name": "Regular", "last_name": "Test",
            "email": "regular@test.com", "phone": "22222", "two_factor_method": "none",
        }
        payload.update(overrides)
        return self.client.put(f"/api/v1/admin/users/{self.regular_user.id}", json=payload)

    def test_self_edit_rejects_html_in_email(self):
        # Security review 2026-11-07 HIGH 2: stored XSS via own profile email.
        r = self._self_edit(email="<img src=x onerror=alert(1)>@x.com")
        self.assertEqual(r.status_code, 400)

    def test_self_edit_rejects_html_in_phone(self):
        r = self._self_edit(phone='"><svg onload=alert(1)>')
        self.assertEqual(r.status_code, 400)

    def test_self_edit_rejects_unknown_two_factor_method(self):
        # two_factor_method is rendered in the admin table; only known values allowed.
        r = self._self_edit(two_factor_method="<b>x</b>")
        self.assertEqual(r.status_code, 400)

    def test_self_edit_accepts_normal_contact(self):
        r = self._self_edit(email="first.last+tag@example.co.uk", phone="+1 (555) 010-0199")
        self.assertEqual(r.status_code, 200)

    def test_create_user_rejects_html_in_email(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        r = self.client.post("/api/v1/admin/users", json={
            "username": "xss_probe_user", "password": "pw123456", "first_name": "A", "last_name": "B",
            "email": "<script>@x.com",
        })
        self.assertEqual(r.status_code, 400)

    def test_edit_user_forbidden(self):
        # Regular user cannot edit admin user's details
        app.dependency_overrides[get_current_active_user] = lambda: self.regular_user
        payload = {
            "first_name": "Attacker",
            "last_name": "Hacker",
            "email": "hack@test.com",
            "phone": "00000",
            "role": "user",
            "two_factor_method": "none"
        }
        response = self.client.put(f"/api/v1/admin/users/{self.admin_user.id}", json=payload)
        self.assertEqual(response.status_code, 403)

    def test_delete_user_as_admin(self):
        # Admin deletes regular user
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.delete(f"/api/v1/admin/users/{self.regular_user.id}")
        self.assertEqual(response.status_code, 200)
        
        async def verify():
            async with AsyncSessionLocal() as session:
                res = await session.execute(select(User).where(User.id == self.regular_user.id))
                self.assertIsNone(res.scalar_one_or_none())
        self.loop.run_until_complete(verify())

    def test_delete_user_as_themselves(self):
        # Regular user deletes their own account
        app.dependency_overrides[get_current_active_user] = lambda: self.regular_user
        response = self.client.delete(f"/api/v1/admin/users/{self.regular_user.id}")
        self.assertEqual(response.status_code, 200)

        async def verify():
            async with AsyncSessionLocal() as session:
                res = await session.execute(select(User).where(User.id == self.regular_user.id))
                self.assertIsNone(res.scalar_one_or_none())
        self.loop.run_until_complete(verify())

    def test_delete_user_forbidden(self):
        # Regular user cannot delete admin
        app.dependency_overrides[get_current_active_user] = lambda: self.regular_user
        response = self.client.delete(f"/api/v1/admin/users/{self.admin_user.id}")
        self.assertEqual(response.status_code, 403)

    def _create_payload(self, **overrides):
        payload = {
            "username": "test_new_created_user",
            "password": "CreatedPass123",
            "first_name": "New",
            "last_name": "User",
            "email": "new-user@test.com",
            "phone": None,
            "role": "user",
            "two_factor_method": "none",
        }
        payload.update(overrides)
        return payload

    def _cleanup_created_user(self):
        async def _cleanup():
            async with AsyncSessionLocal() as session:
                await session.execute(delete(User).where(User.username == "test_new_created_user"))
                await session.commit()
        self.loop.run_until_complete(_cleanup())

    def test_create_user_as_admin_success(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        self.addCleanup(self._cleanup_created_user)
        response = self.client.post("/api/v1/admin/users", json=self._create_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["username"], "test_new_created_user")

    def test_create_user_forbidden_for_non_admin(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.regular_user
        response = self.client.post("/api/v1/admin/users", json=self._create_payload())
        self.assertEqual(response.status_code, 403)

    def test_create_user_missing_first_name(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.post("/api/v1/admin/users", json=self._create_payload(first_name="  "))
        self.assertEqual(response.status_code, 400)

    def test_create_user_missing_last_name(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.post("/api/v1/admin/users", json=self._create_payload(last_name=""))
        self.assertEqual(response.status_code, 400)

    def test_create_user_missing_contact_info(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.post("/api/v1/admin/users", json=self._create_payload(email=None, phone=None))
        self.assertEqual(response.status_code, 400)

    def test_create_user_email_2fa_requires_email(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.post(
            "/api/v1/admin/users",
            json=self._create_payload(email=None, phone="5551234", two_factor_method="email"),
        )
        self.assertEqual(response.status_code, 400)

    def test_create_user_phone_2fa_requires_phone(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.post(
            "/api/v1/admin/users",
            json=self._create_payload(phone=None, two_factor_method="phone"),
        )
        self.assertEqual(response.status_code, 400)

    def test_create_user_duplicate_username(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.post(
            "/api/v1/admin/users", json=self._create_payload(username="test_admin_user")
        )
        self.assertEqual(response.status_code, 400)

    def test_update_user_role_as_admin(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.put(f"/api/v1/admin/users/{self.regular_user.id}/role", json={"role": "admin"})
        self.assertEqual(response.status_code, 200)

        async def verify():
            async with AsyncSessionLocal() as session:
                res = await session.execute(select(User).where(User.id == self.regular_user.id))
                self.assertEqual(res.scalar_one().role, "admin")
        self.loop.run_until_complete(verify())

    def test_update_user_role_not_found(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.put("/api/v1/admin/users/does-not-exist/role", json={"role": "admin"})
        self.assertEqual(response.status_code, 404)

    def test_update_user_role_cannot_demote_admin(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.put(f"/api/v1/admin/users/{self.admin_user.id}/role", json={"role": "user"})
        self.assertEqual(response.status_code, 400)

    def test_admin_reset_user_password_success(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.put(
            f"/api/v1/admin/users/{self.regular_user.id}/password",
            json={"new_password": "AdminSetPass123"},
        )
        self.assertEqual(response.status_code, 200)

        async def verify():
            async with AsyncSessionLocal() as session:
                res = await session.execute(select(User).where(User.id == self.regular_user.id))
                u = res.scalar_one()
                self.assertTrue(u.must_change_password)
        self.loop.run_until_complete(verify())

    def test_admin_reset_user_password_too_short(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.put(
            f"/api/v1/admin/users/{self.regular_user.id}/password", json={"new_password": "ab"}
        )
        self.assertEqual(response.status_code, 400)

    def test_admin_reset_user_password_not_found(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.put(
            "/api/v1/admin/users/does-not-exist/password", json={"new_password": "AdminSetPass123"}
        )
        self.assertEqual(response.status_code, 404)

    def test_edit_user_not_found(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.put(
            "/api/v1/admin/users/does-not-exist",
            json={
                "first_name": "X", "last_name": "Y", "email": "x@y.com",
                "phone": None, "role": "user", "two_factor_method": "none",
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_edit_user_missing_contact_info(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.put(
            f"/api/v1/admin/users/{self.regular_user.id}",
            json={
                "first_name": "X", "last_name": "Y", "email": None,
                "phone": None, "role": "user", "two_factor_method": "none",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_edit_user_cannot_demote_last_admin(self):
        # self.admin_user is the only admin in this isolated test DB at this
        # point (test_regular_user is 'user' role) - demoting it should hit
        # the "last remaining admin" guard, not the generic demote guard.
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.put(
            f"/api/v1/admin/users/{self.admin_user.id}",
            json={
                "first_name": "Admin", "last_name": "Test", "email": "admin@test.com",
                "phone": "11111", "role": "user", "two_factor_method": "none",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("last remaining admin", response.json()["detail"])

    def test_delete_user_not_found(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.delete("/api/v1/admin/users/does-not-exist")
        self.assertEqual(response.status_code, 404)

    def test_delete_last_admin_forbidden(self):
        app.dependency_overrides[get_current_active_user] = lambda: self.admin_user
        response = self.client.delete(f"/api/v1/admin/users/{self.admin_user.id}")
        self.assertEqual(response.status_code, 400)
        self.assertIn("last remaining admin", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
