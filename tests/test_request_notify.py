"""'Your request is ready' emails: request_status parsing + request_notify sweep + prefs API."""
import asyncio
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from media_manager.database import AsyncSessionLocal
from media_manager.models import AppLogEvent, IntegrationConfig, RequestNotification, User
from media_manager.web.api import integrations as im
from media_manager.web.app import app
from media_manager.web.services import request_notify, request_status
from media_manager.web.services.auth import create_access_token, hash_password

USERS = ("rn_alice", "rn_bob", "rn_carol")
TAG = "[MEDIA_REQUEST]"


def _ev(username, title, tmdb_id=None, tvdb_id=None, created_at=None, message=None, context=True):
    ev = AppLogEvent(level="INFO", event_tag=TAG, created_at=created_at or datetime.utcnow())
    kind = "TV show" if tvdb_id else "movie"
    ev.message = message or f"User '{username}' requested {kind} '{title}'"
    if context:
        ctx = {"username": username, "title": title}
        if tmdb_id is not None:
            ctx["tmdb_id"] = tmdb_id
        if tvdb_id is not None:
            ctx["tvdb_id"] = tvdb_id
        ev.context_json = json.dumps(ctx)
    return ev


class TestParseRequestEvents(unittest.TestCase):
    def test_dedupes_per_user_keeping_newest(self):
        now = datetime.utcnow()
        events = [
            _ev("a", "Film", tmdb_id=1, created_at=now),
            _ev("a", "Film", tmdb_id=1, created_at=now - timedelta(days=1)),
            _ev("b", "Film", tmdb_id=1, created_at=now - timedelta(days=2)),
        ]
        out = request_status.parse_request_events(events)
        self.assertEqual([(r["username"], r["requested_at"]) for r in out], [("a", now), ("b", now - timedelta(days=2))])
        self.assertEqual(out[0]["dedupe_key"], "tmdb:1")

    def test_username_filter_and_keys(self):
        events = [_ev("a", "Show", tvdb_id=7), _ev("b", "Other", tmdb_id=2), _ev("a", " No Ids ")]
        out = request_status.parse_request_events(events, username="a")
        self.assertEqual([r["dedupe_key"] for r in out], ["tvdb:7", "title:no ids"])
        self.assertEqual(out[0]["media_type"], "tv")

    def test_legacy_message_only_event(self):
        ev = _ev("x", "ignored", context=False,
                 message="User 'old' requested TV show 'Legacy' (TMDb: 5) (TVDb: 9)")
        (r,) = request_status.parse_request_events([ev])
        self.assertEqual((r["username"], r["title"], r["media_type"], r["tmdb_id"], r["tvdb_id"]),
                         ("old", "Legacy", "tv", 5, 9))

    def test_unparseable_events_skipped(self):
        bad_json = _ev("x", "t", context=False, message="something else")
        bad_json.context_json = "{not json"
        self.assertEqual(request_status.parse_request_events([bad_json]), [])


def _radarr(movies, queue=()):
    inst = MagicMock()
    inst.get_movies = AsyncMock(return_value=list(movies))
    inst.get_queue = AsyncMock(return_value=list(queue))
    return MagicMock(return_value=inst)


def _sonarr(series, queue=()):
    inst = MagicMock()
    inst.get_series = AsyncMock(return_value=list(series))
    inst.get_queue = AsyncMock(return_value=list(queue))
    return MagicMock(return_value=inst)


class TestRequestNotify(unittest.TestCase):
    def setUp(self):
        asyncio.run(self._reset(seed=True))

    def tearDown(self):
        asyncio.run(self._reset(seed=False))

    async def _reset(self, seed):
        async with AsyncSessionLocal() as db:
            await db.execute(delete(RequestNotification))
            await db.execute(delete(AppLogEvent).where(AppLogEvent.event_tag == TAG))
            await db.execute(delete(User).where(User.username.in_(USERS)))
            await db.execute(delete(IntegrationConfig).where(
                IntegrationConfig.service_name.in_(("email", "radarr", "sonarr"))))
            if seed:
                db.add_all([
                    IntegrationConfig(service_name="email", api_url="smtp.test:25:mm@test:false", api_key_encrypted=""),
                    IntegrationConfig(service_name="radarr", api_url="http://radarr.test", api_key_encrypted="k"),
                    IntegrationConfig(service_name="sonarr", api_url="http://sonarr.test", api_key_encrypted="k"),
                    User(username="rn_alice", first_name="Alice", email="alice@test", role="user"),
                    User(username="rn_bob", email="bob@test", role="user", notify_on_available=False),
                    User(username="rn_carol", email=None, role="user"),
                ])
            await db.commit()

    async def _add_events(self, *events):
        async with AsyncSessionLocal() as db:
            db.add_all(events)
            await db.commit()

    async def _sweep(self, radarr=None, sonarr=None, send=True):
        sender = AsyncMock(return_value=send)
        with patch.object(im, "RadarrClient", radarr or _radarr([])), \
                patch.object(im, "SonarrClient", sonarr or _sonarr([])), \
                patch("media_manager.web.services.email.send_email_notification", sender):
            async with AsyncSessionLocal() as db:
                result = await request_notify.run_request_notifications(db)
        return result, sender

    async def _rows(self):
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(RequestNotification).where(RequestNotification.username != request_notify.BASELINE))
            return {(r.username, r.dedupe_key): r.emailed for r in res.scalars().all()}

    def _baseline(self):
        result, sender = asyncio.run(self._sweep())
        self.assertEqual(result, {"baseline": True})
        sender.assert_not_called()

    def test_email_disabled_short_circuits(self):
        async def disable():
            async with AsyncSessionLocal() as db:
                cfg = (await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "email"))).scalar_one()
                cfg.enabled = False
                await db.commit()
        asyncio.run(disable())
        result, sender = asyncio.run(self._sweep())
        self.assertEqual(result, {"skipped": "email disabled"})

    def test_requests_before_baseline_never_emailed(self):
        asyncio.run(self._add_events(_ev("rn_alice", "Old Film", tmdb_id=11, created_at=datetime.utcnow() - timedelta(days=3))))
        self._baseline()
        result, sender = asyncio.run(self._sweep(radarr=_radarr([{"id": 1, "tmdbId": 11, "hasFile": True}])))
        self.assertEqual(result, {"checked": 0, "emailed": 0})
        sender.assert_not_called()

    def test_available_movie_emailed_once(self):
        self._baseline()
        asyncio.run(self._add_events(_ev("rn_alice", "New Film", tmdb_id=21)))
        radarr = _radarr([{"id": 1, "tmdbId": 21, "hasFile": True}])
        result, sender = asyncio.run(self._sweep(radarr=radarr))
        self.assertEqual(result, {"checked": 1, "emailed": 1})
        to, subject, body = sender.await_args.args
        self.assertEqual(to, "alice@test")
        self.assertEqual(subject, "Ready to watch: New Film")
        self.assertIn("Hi Alice", body)
        self.assertEqual(asyncio.run(self._rows()), {("rn_alice", "tmdb:21"): True})

        result, sender = asyncio.run(self._sweep(radarr=radarr))
        self.assertEqual(result, {"checked": 0, "emailed": 0})
        sender.assert_not_called()

    def test_not_yet_available_waits(self):
        self._baseline()
        asyncio.run(self._add_events(_ev("rn_alice", "Pending Film", tmdb_id=31),
                                     _ev("rn_alice", "Queued Show", tvdb_id=41)))
        result, sender = asyncio.run(self._sweep(
            radarr=_radarr([{"id": 1, "tmdbId": 31, "hasFile": False}]),
            sonarr=_sonarr([{"id": 2, "tvdbId": 41, "statistics": {"episodeFileCount": 0}}], queue=[{"seriesId": 2}])))
        self.assertEqual(result, {"checked": 2, "emailed": 0})
        sender.assert_not_called()
        self.assertEqual(asyncio.run(self._rows()), {})

    def test_failed_send_retried_next_sweep(self):
        self._baseline()
        asyncio.run(self._add_events(_ev("rn_alice", "Retry Show", tvdb_id=51)))
        sonarr = _sonarr([{"id": 2, "tvdbId": 51, "statistics": {"episodeFileCount": 3}}])
        result, sender = asyncio.run(self._sweep(sonarr=sonarr, send=False))
        self.assertEqual(result["emailed"], 0)
        self.assertEqual(asyncio.run(self._rows()), {})
        result, sender = asyncio.run(self._sweep(sonarr=sonarr))
        self.assertEqual(result["emailed"], 1)
        self.assertIn("TV show", sender.await_args.args[2])

    def test_opted_out_no_email_and_unknown_users_recorded_without_send(self):
        self._baseline()
        asyncio.run(self._add_events(_ev("rn_bob", "Bob Film", tmdb_id=61),
                                     _ev("rn_carol", "Carol Film", tmdb_id=61),
                                     _ev("rn_ghost", "Ghost Film", tmdb_id=61)))
        result, sender = asyncio.run(self._sweep(radarr=_radarr([{"id": 1, "tmdbId": 61, "hasFile": True}])))
        self.assertEqual(result, {"checked": 3, "emailed": 0})
        sender.assert_not_called()
        self.assertEqual(asyncio.run(self._rows()),
                         {("rn_bob", "tmdb:61"): False, ("rn_carol", "tmdb:61"): False, ("rn_ghost", "tmdb:61"): False})

    def test_build_email_flattens_newlines(self):
        user = User(username="u", first_name=None)
        subject, body = request_notify.build_email({"title": "Bad\r\nBcc: x", "media_type": "movie"}, user)
        self.assertEqual(subject, "Ready to watch: Bad Bcc: x")
        self.assertIn("Hi u,", body)

    def test_scheduler_swallows_errors(self):
        calls = []

        async def fake_run(db):
            calls.append(1)
            raise RuntimeError("boom")

        async def fake_sleep(_):
            if calls:
                raise asyncio.CancelledError()

        with patch.object(request_notify, "run_request_notifications", fake_run), \
                patch.object(request_notify.asyncio, "sleep", fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(request_notify.request_notify_scheduler())
        self.assertEqual(calls, [1])


class TestNotificationPrefsApi(unittest.TestCase):
    USERNAME = "rn_prefs"

    def setUp(self):
        self.client = TestClient(app)
        asyncio.run(self._reset(create=True))

    def tearDown(self):
        asyncio.run(self._reset(create=False))

    async def _reset(self, create):
        async with AsyncSessionLocal() as db:
            await db.execute(delete(User).where(User.username == self.USERNAME))
            if create:
                u = User(username=self.USERNAME, email=None, role="user", password_hash=hash_password("pw-123456"))
                db.add(u)
                await db.flush()
                self.token = create_access_token({"sub": u.id, "username": u.username, "role": "user", "tv": 0})
            await db.commit()

    def test_me_reports_and_put_updates_pref(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        me = self.client.get("/api/v1/auth/me", headers=headers).json()["user"]
        self.assertTrue(me["notify_on_available"])
        self.assertFalse(me["has_email"])

        res = self.client.put("/api/v1/auth/me/notifications", json={"notify_on_available": False}, headers=headers)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"notify_on_available": False, "has_email": False})
        me = self.client.get("/api/v1/auth/me", headers=headers).json()["user"]
        self.assertFalse(me["notify_on_available"])

    def test_put_requires_login(self):
        res = TestClient(app).put("/api/v1/auth/me/notifications", json={"notify_on_available": False})
        self.assertEqual(res.status_code, 401)


if __name__ == "__main__":
    unittest.main()
