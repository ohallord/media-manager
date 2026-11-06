import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete, select

from media_manager.database import AsyncSessionLocal
from media_manager.models import IntegrationConfig, TranscodeControl
from media_manager.web.services import integration_health as health
from media_manager.web.services.transcode_planner import get_or_create_control

DL_DOWN = [{"source": "DownloadClientCheck", "type": "error", "message": "Unable to communicate with SABnzbd."}]


def _db(fn):
    async def run():
        async with AsyncSessionLocal() as db:
            return await fn(db)
    return asyncio.run(run())


class HealthBase(unittest.TestCase):
    def setUp(self):
        _db(self._reset)
        health._streaks.clear()
        self.sent = []
        self.mail = patch("media_manager.web.services.email.send_email_notification",
                          AsyncMock(side_effect=lambda to, subject, body: self.sent.append((to, subject, body)) or True))
        self.mail.start()

        async def control(db):
            c = await get_or_create_control(db)
            c.notify_emails = "ops@example.com"
            await db.commit()
        _db(control)

    def tearDown(self):
        self.mail.stop()
        _db(self._reset)
        health._streaks.clear()

    async def _reset(self, db):
        await db.execute(delete(IntegrationConfig).where(IntegrationConfig.service_name.in_(list(health.CLIENTS))))
        await db.execute(delete(TranscodeControl))
        await db.commit()

    def _config(self, service, status="HEALTHY", enabled=True, api_url="http://svc:1"):
        async def run(db):
            db.add(IntegrationConfig(service_name=service, api_url=api_url, api_key_encrypted="k",
                                     enabled=enabled, status=status))
            await db.commit()
        _db(run)

    def _status(self, service):
        async def run(db):
            return (await db.execute(select(IntegrationConfig.status)
                                     .where(IntegrationConfig.service_name == service))).scalar_one()
        return _db(run)

    def _run(self, up=True, health_data=None):
        """One scheduler sweep with every client's status/health mocked."""
        with patch.object(health.RadarrClient, "get_status", AsyncMock(return_value=up)), \
             patch.object(health.SonarrClient, "get_status", AsyncMock(return_value=up)), \
             patch.object(health.SABnzbdClient, "get_status", AsyncMock(return_value=up)), \
             patch.object(health.RadarrClient, "get_health", AsyncMock(return_value=health_data or [])), \
             patch.object(health.SonarrClient, "get_health", AsyncMock(return_value=health_data or [])):
            return _db(health.run_health_checks)


class TestDownloadClientIssues(unittest.TestCase):
    def test_only_download_client_checks_count(self):
        data = DL_DOWN + [{"source": "DownloadClientStatusCheck", "message": "All download clients are unavailable"},
                          {"source": "IndexerStatusCheck", "message": "Indexers unavailable"},
                          {"source": "DownloadClientCheck"}, "junk"]
        self.assertEqual(health.download_client_issues(data),
                         ["Unable to communicate with SABnzbd.", "All download clients are unavailable",
                          "DownloadClientCheck"])

    def test_unreadable_health_is_not_an_issue(self):
        self.assertEqual(health.download_client_issues(None), [])

    def test_no_email_for_unknown_to_healthy(self):
        self.assertIsNone(health.alert_text("radarr", "UNKNOWN", "HEALTHY", []))


class TestRadarrDownloadClient(HealthBase):
    def test_alerts_once_per_outage_then_once_on_recovery(self):
        self._config("radarr")
        self.assertEqual(self._run(health_data=DL_DOWN), [])  # first bad check: wait for a second
        self.assertEqual(self._run(health_data=DL_DOWN), ["Radarr can't reach its download client"])
        self.assertIn("Unable to communicate with SABnzbd.", self.sent[0][2])
        self.assertEqual(self.sent[0][0], "ops@example.com")
        self.assertEqual(self._status("radarr"), "DOWNLOAD_CLIENT_ERROR")
        for _ in range(3):
            self.assertEqual(self._run(health_data=DL_DOWN), [])
        self.assertEqual(self._run(), ["Radarr recovered"])
        self.assertIn("download client again", self.sent[1][2])
        self.assertEqual(self._status("radarr"), "HEALTHY")
        self.assertEqual(self._run(), [])
        self.assertEqual(len(self.sent), 2)

    def test_single_blip_does_not_alert(self):
        self._config("radarr")
        self._run(health_data=DL_DOWN)
        self._run()
        self._run(health_data=DL_DOWN)
        self.assertEqual(self.sent, [])


class TestServiceDown(HealthBase):
    def test_sonarr_and_sabnzbd_down_then_recover(self):
        self._config("sonarr")
        self._config("sabnzbd")
        self._run(up=False)
        self.assertEqual(sorted(self._run(up=False)), ["SABnzbd is down", "Sonarr is down"])
        self.assertEqual(self._run(up=False), [])
        self.assertEqual(self._status("sabnzbd"), "UNREACHABLE")
        self.assertEqual(sorted(self._run()), ["SABnzbd recovered", "Sonarr recovered"])
        self.assertIn("is back up", self.sent[-1][2])
        self.assertEqual(len(self.sent), 4)

    def test_down_survives_restart_without_realerting(self):
        self._config("sabnzbd", status="UNREACHABLE")  # alerted before the pod restarted
        self._run(up=False)
        self.assertEqual(self._run(up=False), [])

    def test_down_to_download_client_error_sends_that_alert(self):
        self._config("sonarr", status="UNREACHABLE")
        self._run(health_data=DL_DOWN)
        self.assertEqual(self._run(health_data=DL_DOWN), ["Sonarr can't reach its download client"])

    def test_first_healthy_check_is_silent(self):
        self._config("radarr", status="UNKNOWN")
        self.assertEqual(self._run(), [])
        self.assertEqual(self._status("radarr"), "HEALTHY")

    def test_disabled_or_unset_services_skipped(self):
        self._config("radarr", enabled=False)
        self._config("sonarr", api_url="")
        self._run(up=False)
        self.assertEqual(self._run(up=False), [])

    def test_check_exception_is_skipped(self):
        self._config("radarr")
        with patch.object(health, "check_service", AsyncMock(side_effect=RuntimeError("boom"))):
            self.assertEqual(_db(health.run_health_checks), [])
        self.assertEqual(self._status("radarr"), "HEALTHY")

    def test_no_recipients_sends_nothing(self):
        self._config("sabnzbd")
        with patch("media_manager.web.services.transcode_notify.recipients", AsyncMock(return_value=[])):
            self._run(up=False)
            self.assertEqual(self._run(up=False), [])
        self.assertEqual(self._status("sabnzbd"), "UNREACHABLE")


if __name__ == "__main__":
    unittest.main()
