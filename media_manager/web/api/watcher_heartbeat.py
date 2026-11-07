import hmac
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.database import get_async_db
from media_manager.heartbeat import WATCHER_SERVICE, watcher_token
from media_manager.models import ServiceHeartbeat

router = APIRouter(prefix="/api/v1/watcher", tags=["watcher"])


@router.post("/heartbeat")
async def ingest_watcher_heartbeat(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    x_watcher_token: str = Header(default="", alias="X-Watcher-Token"),
):
    """
    Host `media-manager-watcher.service` posts here so the in-cluster web app can
    show liveness even when SQLite files are not shared.
    """
    expected = watcher_token()
    if not expected:
        raise HTTPException(status_code=503, detail="WATCHER_HEARTBEAT_TOKEN is not configured")
    got = x_watcher_token or ""
    try:
        token_ok = hmac.compare_digest(got, expected)
    except (TypeError, ValueError):
        token_ok = False
    if not token_ok:
        raise HTTPException(status_code=401, detail="Invalid watcher token")

    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    res = await db.execute(select(ServiceHeartbeat).where(ServiceHeartbeat.service == WATCHER_SERVICE))
    row = res.scalar_one_or_none()
    if row is None:
        row = ServiceHeartbeat(service=WATCHER_SERVICE)
        db.add(row)
    row.last_beat_at = datetime.utcnow()
    if isinstance(payload, dict) and payload.get("pid") is not None:
        try:
            row.pid = int(payload["pid"])
        except (TypeError, ValueError):
            pass
    host = request.client.host if request.client else None
    row.hostname = (payload.get("hostname") if isinstance(payload, dict) else None) or host
    await db.commit()
    return {"ok": True}
