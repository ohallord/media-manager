import smtplib
from email.mime.text import MIMEText
from email.header import Header
from typing import NamedTuple, Optional
from sqlalchemy import select
from media_manager.database import AsyncSessionLocal
from media_manager.models import IntegrationConfig
import logging
import asyncio

logger = logging.getLogger(__name__)


class SmtpConfig(NamedTuple):
    host: str
    port: int
    sender: str
    use_tls: bool


def parse_smtp_config(api_url: str) -> Optional[SmtpConfig]:
    """Parses the 'host:port:sender:use_tls' format IntegrationConfig.api_url uses for email."""
    parts = (api_url or "").split(":")
    if len(parts) < 4:
        return None
    try:
        return SmtpConfig(host=parts[0], port=int(parts[1]), sender=parts[2], use_tls=parts[3].lower() == "true")
    except ValueError:
        return None


def smtp_connect(cfg: SmtpConfig, timeout: int = 10) -> smtplib.SMTP:
    """Blocking - only ever call via an executor/asyncio.to_thread."""
    server = smtplib.SMTP(cfg.host, cfg.port, timeout=timeout)
    if cfg.use_tls:
        server.ehlo()
        server.starttls()
        server.ehlo()
    return server


async def send_email_notification(to_email: str, subject: str, body: str) -> bool:
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "email"))
        cfg = res.scalar_one_or_none()
        if not cfg or not cfg.enabled:
            logger.info("Email notifications are disabled or not configured.")
            return False

        smtp_cfg = parse_smtp_config(cfg.api_url)
        if not smtp_cfg:
            logger.error("Invalid SMTP config format.")
            return False

        try:
            password = cfg.api_key_encrypted

            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = smtp_cfg.sender
            msg["To"] = to_email

            def run_send():
                server = smtp_connect(smtp_cfg)
                if password:
                    server.login(smtp_cfg.sender, password)
                server.sendmail(smtp_cfg.sender, [to_email], msg.as_string())
                server.quit()

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, run_send)
            logger.info(f"Successfully sent email to {to_email} with subject: {subject}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {e}")
            return False
