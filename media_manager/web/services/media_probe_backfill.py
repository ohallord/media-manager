"""
Re-probes existing media_items rows so the rich codec columns
(metadata.DETAIL_FIELDS) are filled in for files indexed before they existed,
or by an older extract_details() (probe_version < PROBE_VERSION).

Runs inside the web pod - it has ffprobe, every media mount, and is the only
writer of the prod DB. Each file goes through scanner.process_file, the same
upsert a normal scan does, so the backfill also corrects fields the old probe
got wrong (e.g. MP4 cover art recorded as the video codec).
"""
import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, or_, select

from media_manager.database import AsyncSessionLocal
from media_manager.metadata import PROBE_VERSION
from media_manager.models import MediaItem
from media_manager.quality import calculate_quality_score, volume_is_healthy
from media_manager.scanner import process_file

logger = logging.getLogger(__name__)

# ffprobe mostly waits on disk/NAS reads; a few in parallel speeds that up
# without flooding the CIFS mounts or SQLite with concurrent writers.
BACKFILL_CONCURRENCY = 3

_status: Dict = {}
_task: Optional[asyncio.Task] = None


def _reset_status() -> None:
    _status.clear()
    _status.update({
        "state": "idle",
        "total": 0,
        "done": 0,
        "failed": 0,
        "skipped_missing": 0,
        "skipped_empty": 0,
        "skipped_unhealthy_volume": 0,
        "rescored": 0,
        "started_at": None,
        "finished_at": None,
        "error": None,
    })


_reset_status()


def get_status() -> Dict:
    return dict(_status)


def is_running() -> bool:
    return _task is not None and not _task.done()


def _needs_probe_clause():
    return or_(MediaItem.probe_version.is_(None), MediaItem.probe_version < PROBE_VERSION)


async def get_coverage() -> Dict[str, int]:
    """How many rows are already probed at the current PROBE_VERSION."""
    async with AsyncSessionLocal() as db:
        total = (await db.execute(select(func.count(MediaItem.id)))).scalar() or 0
        pending = (await db.execute(
            select(func.count(MediaItem.id)).where(_needs_probe_clause())
        )).scalar() or 0
    return {"total_items": total, "pending_items": pending, "probed_items": total - pending}


async def _select_targets() -> List[Tuple[str, str]]:
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(MediaItem.path, MediaItem.storage_volume).where(_needs_probe_clause())
        )
        return [(row[0], row[1]) for row in res.all()]


RESCORE_BATCH_SIZE = 500


async def rescore_all() -> Dict[str, int]:
    """
    Recomputes quality_score for every row from its stored fields, so scores
    follow scoring changes (e.g. codec efficiency) without a full rescan.
    Only rows whose score changed are written, committed in batches.
    """
    changed = 0
    total = 0
    offset = 0
    while True:
        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(MediaItem).order_by(MediaItem.id).offset(offset).limit(RESCORE_BATCH_SIZE)
            )
            batch = res.scalars().all()
            if not batch:
                break
            for item in batch:
                score = calculate_quality_score(
                    resolution=item.resolution or "SD",
                    height=item.height or 0,
                    audio_codec=item.audio_codec or "unknown",
                    audio_channels=item.audio_channels or 2,
                    bitrate=item.bitrate or 0,
                    path=item.path,
                    video_codec=item.video_codec,
                    video_bitrate=item.video_bitrate,
                )
                if score != item.quality_score:
                    item.quality_score = score
                    changed += 1
            await db.commit()
        total += len(batch)
        offset += RESCORE_BATCH_SIZE
    logger.info(f"[PROBE_BACKFILL] Rescored {total} media items, {changed} scores changed.")
    return {"total": total, "changed": changed}


def _probe_one(path: str) -> str:
    if not os.path.exists(path):
        return "missing"
    # 0-byte placeholders can't be probed; reported separately from failures.
    if os.path.getsize(path) == 0:
        return "empty"
    return "done" if process_file(path) else "failed"


async def run_probe_backfill() -> Dict:
    _reset_status()
    _status["state"] = "running"
    _status["started_at"] = datetime.utcnow().isoformat()
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=BACKFILL_CONCURRENCY)
    try:
        targets = await _select_targets()
        _status["total"] = len(targets)
        logger.info(f"[PROBE_BACKFILL] Re-probing {len(targets)} media items (probe v{PROBE_VERSION})...")

        # One health check per volume, not per file: a dropped CIFS mount
        # would otherwise make every one of its files look "missing".
        volumes = {volume for _, volume in targets}
        health = {}
        for volume in volumes:
            health[volume] = await loop.run_in_executor(executor, volume_is_healthy, volume)
        unhealthy = sorted(v for v, ok in health.items() if not ok)
        if unhealthy:
            logger.warning(f"[PROBE_BACKFILL] Skipping unhealthy volumes: {', '.join(unhealthy)}")

        semaphore = asyncio.Semaphore(BACKFILL_CONCURRENCY)

        async def handle(path: str, volume: str) -> None:
            if not health.get(volume):
                _status["skipped_unhealthy_volume"] += 1
                return
            async with semaphore:
                try:
                    outcome = await loop.run_in_executor(executor, _probe_one, path)
                except Exception as e:
                    logger.warning(f"[PROBE_BACKFILL] Failed to probe {path}: {e}")
                    outcome = "failed"
            _status[{"missing": "skipped_missing", "empty": "skipped_empty"}.get(outcome, outcome)] += 1

        await asyncio.gather(*(handle(path, volume) for path, volume in targets))
        # Scores depend on the probed codec/bitrate - bring every row up to date.
        _status["rescored"] = (await rescore_all())["changed"]
        _status["state"] = "completed"
        logger.info(
            f"[PROBE_BACKFILL] Completed: {_status['done']} probed, {_status['failed']} failed, "
            f"{_status['skipped_missing']} missing, {_status['skipped_empty']} empty, "
            f"{_status['skipped_unhealthy_volume']} on unhealthy volumes."
        )
    except Exception as e:
        _status["state"] = "failed"
        _status["error"] = str(e)
        logger.error(f"[PROBE_BACKFILL] Backfill aborted: {e}", exc_info=True)
    finally:
        executor.shutdown(wait=False)
        _status["finished_at"] = datetime.utcnow().isoformat()
    return get_status()


def start_probe_backfill() -> bool:
    """Starts the backfill as a background task. False if one is already running."""
    global _task
    if is_running():
        return False
    _task = asyncio.create_task(run_probe_backfill())
    return True
