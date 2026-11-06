"""Background housekeeping for the H.265 queue, plus Radarr/Sonarr/SABnzbd health alerts (runs in the web app lifespan)."""
import asyncio
import logging
import time

from media_manager.database import AsyncSessionLocal
from media_manager.web.services import transcode_jobs

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 60
AUTO_QUEUE_INTERVAL_SECONDS = 3600  # walks the whole library, so not every sweep
_last_auto_queue = None  # _clock() time of the last run; None = run on the first sweep
_clock = time.monotonic


async def _maybe_auto_queue(db) -> None:
    global _last_auto_queue
    now = _clock()
    if _last_auto_queue is not None and now - _last_auto_queue < AUTO_QUEUE_INTERVAL_SECONDS:
        return
    _last_auto_queue = now
    await transcode_jobs.enqueue_new(db)


async def run_transcode_housekeeping() -> None:
    from media_manager.web.services import integration_health, transcode_notify
    async with AsyncSessionLocal() as db:
        await transcode_jobs.sweep_expired_leases(db)
        for task in (transcode_notify.purge_expired_backups, transcode_notify.run_notifications,
                     integration_health.run_health_checks, _maybe_auto_queue):
            try:
                await task(db)
            except Exception as e:  # one failing task must not stop the others
                logger.error(f"[TRANSCODE] {task.__name__} failed: {e}", exc_info=True)
                await db.rollback()


async def transcode_scheduler():
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await run_transcode_housekeeping()
        except Exception as e:
            logger.error(f"[TRANSCODE] Housekeeping failed: {e}", exc_info=True)
