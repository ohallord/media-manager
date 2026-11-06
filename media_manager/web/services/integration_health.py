"""
Download-stack health alerts, run from the web pod's scheduler (transcode_scheduler):
  - Radarr, Sonarr or SABnzbd fails its status check -> "down" email;
  - Radarr or Sonarr's own health checks say it can't reach its download client
    -> "download client" email;
  - the next healthy check -> one "recovered" email.

The state lives in IntegrationConfig.status (shown on the admin Integrations page),
so an outage alerts once and a pod restart doesn't re-send. A problem must show on
FAILS_BEFORE_ALERT checks in a row first, so one slow response doesn't email.
Recipients are the H.265 notify list (transcode_notify.recipients).
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.integrations.radarr import RadarrClient
from media_manager.integrations.sabnzbd import SABnzbdClient
from media_manager.integrations.sonarr import SonarrClient
from media_manager.models import IntegrationConfig

logger = logging.getLogger(__name__)

CLIENTS = {"radarr": RadarrClient, "sonarr": SonarrClient, "sabnzbd": SABnzbdClient}
TITLES = {"radarr": "Radarr", "sonarr": "Sonarr", "sabnzbd": "SABnzbd"}
HEALTHY, UNREACHABLE, DOWNLOAD_CLIENT_ERROR = "HEALTHY", "UNREACHABLE", "DOWNLOAD_CLIENT_ERROR"
PROBLEMS = (UNREACHABLE, DOWNLOAD_CLIENT_ERROR)
# Radarr/Sonarr health-check sources that mean "can't talk to the download client".
DOWNLOAD_CLIENT_CHECKS = {"DownloadClientCheck", "DownloadClientStatusCheck"}
FAILS_BEFORE_ALERT = 2

_streaks: Dict[str, int] = {}  # service -> consecutive problem checks


def download_client_issues(health: Optional[List[Dict[str, Any]]]) -> List[str]:
    return [h.get("message") or h.get("source") for h in (health or [])
            if isinstance(h, dict) and h.get("source") in DOWNLOAD_CLIENT_CHECKS]


async def check_service(config: IntegrationConfig) -> Dict[str, Any]:
    """{'status': HEALTHY|UNREACHABLE|DOWNLOAD_CLIENT_ERROR, 'issues': [...]}"""
    from media_manager.web.api import integrations as integrations_module
    client = CLIENTS[config.service_name](config.api_url, config.api_key_encrypted,
                                          http_client=integrations_module.SHARED_HTTP_CLIENT)
    if not await client.get_status():
        return {"status": UNREACHABLE, "issues": []}
    if config.service_name == "sabnzbd":
        return {"status": HEALTHY, "issues": []}
    issues = download_client_issues(await client.get_health())
    return {"status": DOWNLOAD_CLIENT_ERROR if issues else HEALTHY, "issues": issues}


def alert_text(service: str, prev: str, new: str, issues: List[str]) -> Optional[tuple]:
    """(subject, body) for a state change worth an email, else None."""
    title = TITLES[service]
    if new == UNREACHABLE:
        return (f"{title} is down",
                f"{title} is not answering its status check, so new downloads and imports are stopped.\n\n"
                "Check its pod/container and the media server. You'll get one more email when it's back.\n")
    if new == DOWNLOAD_CLIENT_ERROR:
        lines = "\n".join(f"  - {i}" for i in issues)
        return (f"{title} can't reach its download client",
                f"{title} is up but reports it can't use its download client:\n\n{lines}\n\n"
                f"Check SABnzbd and the download client settings in {title} (Settings > Download Clients). "
                "You'll get one more email when it's fixed.\n")
    if prev in PROBLEMS:
        what = "is back up" if prev == UNREACHABLE else "can reach its download client again"
        return f"{title} recovered", f"{title} {what}. No action needed.\n"
    return None


async def _notify(db: AsyncSession, subject: str, body: str) -> int:
    from media_manager.web.services import transcode_notify, transcode_planner
    control = await transcode_planner.get_or_create_control(db)
    return await transcode_notify._send(db, control, subject, body)


async def run_health_checks(db: AsyncSession) -> List[str]:
    """Checks each enabled service once; returns the subjects of the emails it sent."""
    configs = (await db.execute(select(IntegrationConfig).where(
        IntegrationConfig.service_name.in_(list(CLIENTS))))).scalars().all()
    sent = []
    for config in configs:
        if not config.enabled or not config.api_url:
            _streaks.pop(config.service_name, None)
            continue
        try:
            result = await check_service(config)
        except Exception as e:
            logger.warning(f"[HEALTH] {config.service_name} check failed: {e}")
            continue
        prev, new = config.status, result["status"]
        if new in PROBLEMS:
            _streaks[config.service_name] = _streaks.get(config.service_name, 0) + 1
            if _streaks[config.service_name] < FAILS_BEFORE_ALERT:
                continue
        else:
            _streaks.pop(config.service_name, None)
        config.last_checked_at = datetime.utcnow()
        if new == prev:
            await db.commit()
            continue
        config.status = new
        await db.commit()
        logger.info(f"[HEALTH] {config.service_name}: {prev} -> {new} {result['issues']}")
        text = alert_text(config.service_name, prev, new, result["issues"])
        if text and await _notify(db, *text) > 0:
            sent.append(text[0])
    return sent
