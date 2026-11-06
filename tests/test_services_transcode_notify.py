import asyncio
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete

from media_manager.database import AsyncSessionLocal
from media_manager.models import ServiceHeartbeat, TranscodeControl, TranscodeJob, TranscodeSettings, User
from media_manager.web.services import transcode_notify as notify
from media_manager.web.services.transcode_planner import get_or_create_control

NOW = datetime.utcnow()


def _db(fn):
    async def run():
        async with AsyncSessionLocal() as db:
            return await fn(db)
    return asyncio.run(run())


class NotifyBase(unittest.TestCase):
    def setUp(self):
        _db(self._reset)
        self.sent = []
        self.mail = patch("media_manager.web.services.email.send_email_notification",
                          AsyncMock(side_effect=lambda to, subject, body: self.sent.append((to, subject, body)) or True))
        self.mail.start()

    def tearDown(self):
        self.mail.stop()
        _db(self._reset)

    async def _reset(self, db):
        await db.execute(delete(TranscodeJob))
        await db.execute(delete(TranscodeControl))
        await db.execute(delete(TranscodeSettings))
        await db.execute(delete(ServiceHeartbeat).where(ServiceHeartbeat.service == "transcoder"))
        await db.execute(delete(User).where(User.username.like("_ut_notify_%")))
        await db.commit()

    def _control(self, **kw):
        async def run(db):
            c = await get_or_create_control(db)
            for k, v in dict(dict(run_state="running", mode="full", notify_emails="ops@example.com"), **kw).items():
                setattr(c, k, v)
            await db.commit()
        _db(run)

    def _job(self, state, minutes_ago=5, **kw):
        async def run(db):
            job = TranscodeJob(state=state, phase="done", source_path=kw.pop("source_path", "/m/x.mp4"),
                               source_size=kw.pop("source_size", 10 * 10**9), title=kw.pop("title", "X"),
                               media_type="movie", finished_at=NOW - timedelta(minutes=minutes_ago), **kw)
            db.add(job)
            await db.commit()
            return job.id
        return _db(run)

    def _run(self, check):
        async def run(db):
            control = await get_or_create_control(db)
            return await check(db, control)
        return _db(run)


class TestRecipients(NotifyBase):
    def test_configured_list_else_admin_emails(self):
        self._control(notify_emails=" a@x.com, b@x.com ,")
        self.assertEqual(self._run(notify.recipients), ["a@x.com", "b@x.com"])

        async def add_users(db):
            db.add(User(username="_ut_notify_admin", role="admin", email="admin@x.com", is_active=True))
            db.add(User(username="_ut_notify_user", role="user", email="user@x.com", is_active=True))
            db.add(User(username="_ut_notify_noemail", role="admin", email=None, is_active=True))
            await db.commit()
        _db(add_users)
        self._control(notify_emails="")
        self.assertIn("admin@x.com", self._run(notify.recipients))
        self.assertNotIn("user@x.com", self._run(notify.recipients))

    def test_send_counts_and_survives_mail_errors(self):
        self._control(notify_emails="a@x.com,b@x.com")
        with patch("media_manager.web.services.email.send_email_notification",
                   AsyncMock(side_effect=[True, RuntimeError("smtp down")])):
            sent = self._run(lambda db, c: notify._send(db, c, "s", "b"))
        self.assertEqual(sent, 1)


class TestDailySummary(NotifyBase):
    def test_sends_once_per_day_with_numbers(self):
        self._control()
        self._job("done", output_size=4 * 10**9, title="Big Movie")
        self._job("skipped", error_code="not_smaller", title="Tiny")
        self._job("pending")
        self.assertTrue(self._run(notify.maybe_send_daily_summary))
        to, subject, body = self.sent[0]
        self.assertEqual(to, "ops@example.com")
        self.assertIn("1 converted, 6.0 GB saved", subject)
        self.assertIn("Still queued: 1", body)
        self.assertIn("Tiny: not_smaller", body)
        self.assertFalse(self._run(notify.maybe_send_daily_summary))  # not again within 24h
        self.assertEqual(len(self.sent), 1)

    def test_quiet_when_nothing_happened_or_off(self):
        self._control()
        self.assertFalse(self._run(notify.maybe_send_daily_summary))
        self._control(mode="off", last_daily_summary_at=None)
        self._job("done", output_size=1)
        self.assertFalse(self._run(notify.maybe_send_daily_summary))
        self.assertEqual(self.sent, [])


class TestFailureStreak(NotifyBase):
    def test_alerts_once_per_streak(self):
        self._control(failure_streak_threshold=3)
        self._job("done", minutes_ago=30, output_size=1)
        for i in range(2):
            self._job("failed", minutes_ago=20 - i, error_code="copy_failed", title=f"F{i}")
        self.assertFalse(self._run(notify.maybe_alert_failure_streak))  # only 2 problems in the last 3
        self._job("skipped", minutes_ago=1, error_code="verify_failed", title="V")
        self.assertTrue(self._run(notify.maybe_alert_failure_streak))
        self.assertIn("3 failures in a row", self.sent[0][1])
        self.assertFalse(self._run(notify.maybe_alert_failure_streak))  # same streak
        self._job("skipped", minutes_ago=0, error_code="not_smaller")  # normal skip breaks the streak
        self.assertFalse(self._run(notify.maybe_alert_failure_streak))

    def test_needs_enough_history(self):
        self._control(failure_streak_threshold=5)
        self._job("failed", error_code="copy_failed")
        self.assertFalse(self._run(notify.maybe_alert_failure_streak))


class TestWorkerSilent(NotifyBase):
    def _beat(self, minutes_ago):
        async def run(db):
            await db.execute(delete(ServiceHeartbeat).where(ServiceHeartbeat.service == "transcoder"))
            db.add(ServiceHeartbeat(service="transcoder", last_beat_at=datetime.utcnow() - timedelta(minutes=minutes_ago)))
            await db.commit()
        _db(run)

    def test_once_per_outage(self):
        self._control()
        self._beat(1)
        self.assertFalse(self._run(notify.maybe_alert_worker_silent))
        self._beat(30)
        self.assertTrue(self._run(notify.maybe_alert_worker_silent))
        self.assertIn("not responding", self.sent[0][1])
        self.assertFalse(self._run(notify.maybe_alert_worker_silent))  # same outage
        self._beat(0)  # came back...
        self.assertFalse(self._run(notify.maybe_alert_worker_silent))
        self._beat(-1)  # (clock skew guard: beat after the alert) then silent again
        async def age(db):
            hb = await db.get(ServiceHeartbeat, "transcoder")
            hb.last_beat_at = datetime.utcnow() - timedelta(minutes=30)
            c = await get_or_create_control(db)
            c.last_silent_alert_at = hb.last_beat_at - timedelta(minutes=5)
            await db.commit()
        _db(age)
        self.assertTrue(self._run(notify.maybe_alert_worker_silent))

    def test_never_seen_and_not_running(self):
        self._control()
        self.assertTrue(self._run(notify.maybe_alert_worker_silent))
        self.assertIn("never checked in", self.sent[0][2])
        self._control(run_state="paused", last_silent_alert_at=None)
        self.assertFalse(self._run(notify.maybe_alert_worker_silent))


class TestPurgeBackups(NotifyBase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="mm_purge_")
        open(os.path.join(self.tmp, ".keep"), "w").close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def _backup(self, name, days_ago):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(b"x" * 100)
        job_id = self._job("done", minutes_ago=days_ago * 24 * 60, backup_path=path, output_size=1)
        return path, job_id

    def test_purges_only_expired_on_healthy_volumes(self):
        old, _ = self._backup("old.mm-orig", days_ago=8)
        fresh, _ = self._backup("fresh.mm-orig", days_ago=1)
        gone, _ = self._backup("gone.mm-orig", days_ago=9)
        os.remove(gone)
        self.assertEqual(_db(notify.retained_backups)["count"], 3)
        with patch.object(notify, "MEDIA_DIRECTORIES", [self.tmp]):
            counts = _db(notify.purge_expired_backups)
        self.assertEqual((counts["purged"], counts["bytes"]), (1, 100))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertEqual(_db(notify.retained_backups)["count"], 1)  # gone one marked purged too

    def test_purges_backup_renamed_by_filebot(self):
        # Job recorded "Movie.mkv.mm-orig"; FileBot's library rename left "Movie.mm-orig".
        recorded = os.path.join(self.tmp, "Movie (2000).mkv.mm-orig")
        self._job("done", minutes_ago=8 * 24 * 60, backup_path=recorded, output_size=1)
        renamed = os.path.join(self.tmp, "Movie (2000).mm-orig")
        with open(renamed, "wb") as fh:
            fh.write(b"x" * 50)
        with patch.object(notify, "MEDIA_DIRECTORIES", [self.tmp]):
            counts = _db(notify.purge_expired_backups)
        self.assertEqual((counts["purged"], counts["bytes"], counts["missing"]), (1, 50, 0))
        self.assertFalse(os.path.exists(renamed))

    def test_missing_backup_is_logged_and_counted(self):
        gone, _ = self._backup("gone.mm-orig", days_ago=9)
        os.remove(gone)
        with patch.object(notify, "MEDIA_DIRECTORIES", [self.tmp]), self.assertLogs(level="WARNING") as logs:
            counts = _db(notify.purge_expired_backups)
        self.assertEqual((counts["purged"], counts["missing"]), (0, 1))
        self.assertTrue(any("not found" in m for m in logs.output))
        # Vanishes between the lookup and the delete.
        old, _ = self._backup("race.mm-orig", days_ago=9)
        with patch.object(notify.os, "remove", side_effect=FileNotFoundError(old)):
            self.assertEqual(_db(notify.purge_expired_backups)["missing"], 1)
        self.assertEqual(_db(notify.retained_backups)["count"], 0)

    def test_unhealthy_volume_and_remove_errors_keep_file(self):
        old, _ = self._backup("old.mm-orig", days_ago=8)
        with patch.object(notify, "volume_is_healthy", return_value=False):
            self.assertEqual(_db(notify.purge_expired_backups)["skipped_unhealthy"], 1)
        with patch.object(notify.os, "remove", side_effect=PermissionError("ro")):
            self.assertEqual(_db(notify.purge_expired_backups)["purged"], 0)
        self.assertTrue(os.path.exists(old))
        self.assertEqual(notify._volume_for("/nowhere/x/y.mkv"), "/nowhere/x")


class TestGpuUnavailable(NotifyBase):
    def _beat(self, minutes_ago=0, **detail):
        async def run(db):
            await db.execute(delete(ServiceHeartbeat).where(ServiceHeartbeat.service == "transcoder"))
            db.add(ServiceHeartbeat(service="transcoder", detail_json=json.dumps(detail),
                                    last_beat_at=datetime.utcnow() - timedelta(minutes=minutes_ago)))
            await db.commit()
        _db(run)

    def test_once_per_outage_with_reboot_hint(self):
        self._control()
        self.assertFalse(self._run(notify.maybe_alert_gpu_unavailable))  # no heartbeat
        self._beat(gpu_error=None)
        self.assertFalse(self._run(notify.maybe_alert_gpu_unavailable))
        self._beat(gpu_error="Failed to initialize NVML: Driver/library version mismatch", auto_reboot=False)
        self.assertTrue(self._run(notify.maybe_alert_gpu_unavailable))
        subject, body = self.sent[0][1], self.sent[0][2]
        self.assertIn("GPU unavailable", subject)
        self.assertIn("version mismatch", body)
        self.assertIn("install_gpu_reboot_timer.sh", body)
        self.assertFalse(self._run(notify.maybe_alert_gpu_unavailable))  # same outage
        self._beat(gpu_error=None)  # recovered: clears the outage
        self.assertFalse(self._run(notify.maybe_alert_gpu_unavailable))
        self._beat(gpu_error="Driver/library version mismatch", auto_reboot=True)
        self.assertTrue(self._run(notify.maybe_alert_gpu_unavailable))
        self.assertIn("03:30", self.sent[1][2])

    def test_stale_heartbeat_or_off_mode_is_silent(self):
        self._control()
        self._beat(30, gpu_error="mismatch")  # worker-silent alert covers this
        self.assertFalse(self._run(notify.maybe_alert_gpu_unavailable))
        self._control(mode="off")
        self._beat(gpu_error="mismatch")
        self.assertFalse(self._run(notify.maybe_alert_gpu_unavailable))
        self.assertEqual(self.sent, [])


class TestRunNotifications(NotifyBase):
    def test_runs_every_check_and_isolates_errors(self):
        self._control()
        with patch.object(notify, "maybe_alert_worker_silent", AsyncMock(side_effect=RuntimeError("x"))), \
             patch.object(notify, "maybe_alert_failure_streak", AsyncMock(return_value=False)) as streak, \
             patch.object(notify, "maybe_alert_gpu_unavailable", AsyncMock(return_value=False)) as gpu, \
             patch.object(notify, "maybe_send_daily_summary", AsyncMock(return_value=False)) as daily:
            _db(notify.run_notifications)
        gpu.assert_awaited_once()
        streak.assert_awaited_once()
        daily.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
