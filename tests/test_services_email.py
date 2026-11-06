import unittest
import asyncio
from unittest.mock import patch, MagicMock
from sqlalchemy import delete

from media_manager.database import AsyncSessionLocal
from media_manager.models import IntegrationConfig
from media_manager.web.services.email import send_email_notification


class TestSendEmailNotification(unittest.TestCase):
    """
    All tests scope their IntegrationConfig rows to service_name == "email"
    and clean up in tearDown - safe against the isolated test DB used by
    scripts/run_tests.sh. Never actually opens a real SMTP connection:
    smtplib.SMTP is always mocked.
    """

    def tearDown(self):
        async def _cleanup():
            async with AsyncSessionLocal() as db:
                await db.execute(delete(IntegrationConfig).where(IntegrationConfig.service_name == "email"))
                await db.commit()
        asyncio.run(_cleanup())

    def _seed_config(self, api_url, api_key="secret", enabled=True):
        async def _seed():
            async with AsyncSessionLocal() as db:
                await db.execute(delete(IntegrationConfig).where(IntegrationConfig.service_name == "email"))
                db.add(IntegrationConfig(
                    service_name="email",
                    api_url=api_url,
                    api_key_encrypted=api_key,
                    enabled=enabled,
                ))
                await db.commit()
        asyncio.run(_seed())

    def test_no_config_returns_false(self):
        result = asyncio.run(send_email_notification("someone@example.com", "Subject", "Body"))
        self.assertFalse(result)

    def test_disabled_config_returns_false(self):
        self._seed_config("smtp.example.com:587:sender@example.com:true", enabled=False)
        result = asyncio.run(send_email_notification("someone@example.com", "Subject", "Body"))
        self.assertFalse(result)

    def test_malformed_config_returns_false(self):
        self._seed_config("smtp.example.com:587")  # missing sender/tls fields
        result = asyncio.run(send_email_notification("someone@example.com", "Subject", "Body"))
        self.assertFalse(result)

    def test_successful_send_with_tls(self):
        self._seed_config("smtp.example.com:587:sender@example.com:true")
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_smtp_cls.return_value = mock_server
            result = asyncio.run(send_email_notification("someone@example.com", "Subject", "Body"))
            self.assertTrue(result)
            mock_server.starttls.assert_called_once()
            mock_server.login.assert_called_once_with("sender@example.com", "secret")
            mock_server.sendmail.assert_called_once()
            mock_server.quit.assert_called_once()

    def test_successful_send_without_tls(self):
        self._seed_config("smtp.example.com:25:sender@example.com:false")
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_smtp_cls.return_value = mock_server
            result = asyncio.run(send_email_notification("someone@example.com", "Subject", "Body"))
            self.assertTrue(result)
            mock_server.starttls.assert_not_called()
            mock_server.sendmail.assert_called_once()

    def test_send_without_password_skips_login(self):
        self._seed_config("smtp.example.com:587:sender@example.com:true", api_key="")
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_smtp_cls.return_value = mock_server
            result = asyncio.run(send_email_notification("someone@example.com", "Subject", "Body"))
            self.assertTrue(result)
            mock_server.login.assert_not_called()

    def test_smtp_exception_returns_false(self):
        self._seed_config("smtp.example.com:587:sender@example.com:true")
        with patch("smtplib.SMTP", side_effect=RuntimeError("connection refused")):
            result = asyncio.run(send_email_notification("someone@example.com", "Subject", "Body"))
            self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
