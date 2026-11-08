"""
"Your request is ready" emails.

A web-pod background sweep (every REQUEST_NOTIFY_INTERVAL_SECONDS) works out
the same status My Requests shows (`request_status`) and emails each
requester once when their title becomes "available". State lives in
`RequestNotification`:
  - the "__baseline__" row is written on the first sweep; requests made before
    it are never emailed, so turning the feature on doesn't mail the back catalogue;
  - one row per (user, title) once handled. A failed send writes no row, so
    the next sweep retries. Users who opted out or have no email get a row with
    emailed=False and are not re-checked.
"""
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.database import AsyncSessionLocal
from media_manager.models import IntegrationConfig, RequestNotification, User
from media_manager.web.services import request_status

logger = logging.getLogger(__name__)

REQUEST_NOTIFY_INTERVAL_SECONDS = 15 * 60
BASELINE = "__baseline__"


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def build_email(request: Dict[str, Any], user: User) -> tuple:
    title = _one_line(request["title"])
    kind = "TV show" if request["media_type"] == "tv" else "movie"
    name = _one_line(user.first_name or user.username)
    subject = f"Ready to watch: {title}"
    body = (
        f"Hi {name},\n\n"
        f"The {kind} you requested, \"{title}\", has been downloaded and is ready to watch.\n\n"
        "You can see all your requests on the My Requests page in Media Manager.\n\n"
        "To stop these emails, untick \"Email me when my requests are ready\" on that page.\n"
    )
    return subject, body


async def _email_enabled(db: AsyncSession) -> bool:
    res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == "email"))
    cfg = res.scalar_one_or_none()
    return bool(cfg and cfg.enabled)


async def run_request_notifications(db: AsyncSession) -> Dict[str, Any]:
    """One sweep. Returns counts for logging/tests."""
    if not await _email_enabled(db):
        return {"skipped": "email disabled"}

    res = await db.execute(select(RequestNotification).where(RequestNotification.username == BASELINE))
    baseline = res.scalar_one_or_none()
    if baseline is None:
        db.add(RequestNotification(username=BASELINE, dedupe_key=BASELINE, notified_at=datetime.utcnow()))
        await db.commit()
        logger.info("[REQUEST_NOTIFY] baseline set; only requests made from now on will be emailed")
        return {"baseline": True}

    requests = [r for r in await request_status.load_requests(db) if r["requested_at"] >= baseline.notified_at]
    if not requests:
        return {"checked": 0, "emailed": 0}
    res = await db.execute(select(RequestNotification.username, RequestNotification.dedupe_key))
    handled = {(u, k) for u, k in res.all()}
    pending = [r for r in requests if (r["username"], r["dedupe_key"]) not in handled]
    if not pending:
        return {"checked": 0, "emailed": 0}

    from media_manager.web.api import integrations as im
    await request_status.apply_statuses(db, pending, im.RadarrClient, im.SonarrClient, im.SHARED_HTTP_CLIENT)
    available = [r for r in pending if r["status"] == "available"]
    if not available:
        return {"checked": len(pending), "emailed": 0}

    names = sorted({r["username"] for r in available})
    res = await db.execute(select(User).where(User.username.in_(names)))
    users = {u.username: u for u in res.scalars().all()}

    from media_manager.web.services.email import send_email_notification
    emailed = 0
    for r in available:
        user = users.get(r["username"])
        wants = bool(user and user.is_active and user.email and user.notify_on_available)
        if wants:
            subject, body = build_email(r, user)
            if not await send_email_notification(user.email, subject, body):
                logger.warning(f"[REQUEST_NOTIFY] Email for '{r['title']}' to {r['username']} failed; will retry")
                continue
            emailed += 1
        db.add(RequestNotification(username=r["username"], dedupe_key=r["dedupe_key"],
                                   title=r["title"][:255], emailed=wants))
    await db.commit()
    if emailed:
        logger.info(f"[REQUEST_NOTIFY] Sent {emailed} 'request ready' email(s)")
    return {"checked": len(pending), "emailed": emailed}


async def request_notify_scheduler():
    while True:
        await asyncio.sleep(REQUEST_NOTIFY_INTERVAL_SECONDS)
        try:
            async with AsyncSessionLocal() as db:
                await run_request_notifications(db)
        except Exception as e:
            logger.error(f"[REQUEST_NOTIFY] Sweep failed: {e}", exc_info=True)
