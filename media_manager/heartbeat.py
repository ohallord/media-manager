import logging
import os
import socket
import ssl
import json
import urllib.request
from datetime import datetime

from media_manager.database import SyncSessionLocal, sync_engine
from media_manager.models import ServiceHeartbeat

logger = logging.getLogger(__name__)

WATCHER_SERVICE = "watcher"
STALE_AFTER_SECONDS = 90
DEFAULT_TOKEN = "media-manager-watcher"
DEFAULT_HEARTBEAT_URL = "http://127.0.0.1:18088/api/v1/watcher/heartbeat"


def watcher_token() -> str:
    return os.getenv("WATCHER_HEARTBEAT_TOKEN") or DEFAULT_TOKEN


def _ensure_table() -> None:
    try:
        ServiceHeartbeat.__table__.create(bind=sync_engine, checkfirst=True)
    except Exception:
        pass


def tick_db() -> None:
    """Write a beat into whatever SQLite/Postgres this process has open."""
    _ensure_table()
    session = SyncSessionLocal()
    try:
        row = session.query(ServiceHeartbeat).filter_by(service=WATCHER_SERVICE).first()
        if row is None:
            row = ServiceHeartbeat(service=WATCHER_SERVICE)
            session.add(row)
        row.last_beat_at = datetime.utcnow()
        row.pid = os.getpid()
        try:
            row.hostname = socket.gethostname()
        except OSError:
            row.hostname = None
        session.commit()
    finally:
        session.close()


def tick_http() -> None:
    """
    POST a beat to the web portal. Needed when the watcher runs on the host
    against checkout media.db while Kubernetes serves /config/media.db.
    """
    flag = os.getenv("WATCHER_HEARTBEAT_HTTP", "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return
    raw = os.getenv("WATCHER_HEARTBEAT_URL") or DEFAULT_HEARTBEAT_URL
    urls = [u.strip() for u in raw.split(",") if u.strip()]
    body = json.dumps({"pid": os.getpid()}).encode("utf-8")
    ctx = ssl.create_default_context()
    verify = os.getenv("WATCHER_HEARTBEAT_TLS_VERIFY", "false").strip().lower()
    if verify in ("0", "false", "no", "off"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    last_err = None
    for target in urls:
        req = urllib.request.Request(
            target,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Watcher-Token": watcher_token(),
            },
        )
        try:
            urllib.request.urlopen(req, timeout=4, context=ctx)
            return
        except Exception as e:
            last_err = e
            logger.warning("Watcher HTTP heartbeat to %s failed: %s", target, e)
    if last_err:
        logger.warning("Watcher HTTP heartbeat gave up after %s URL(s)", len(urls))


def tick_watcher_heartbeat() -> None:
    try:
        tick_db()
    except Exception as e:
        logger.warning("Watcher DB heartbeat tick failed: %s", e)
    try:
        tick_http()
    except Exception as e:
        logger.warning("Watcher HTTP heartbeat tick failed: %s", e)
