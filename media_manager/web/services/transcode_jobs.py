"""
H.265 job queue: enqueue, lease-based claiming by the host worker, write-
ahead phase tracking, completion (DB row updated in place) and failure
handling. The web app is the only DB writer; the worker drives all of this
through web/api/transcode_worker.py.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.metadata import apply_probe_details, metadata_from_probe
from media_manager.models import (
    TRANSCODE_ACTIVE_STATES, TRANSCODE_SWAP_PHASES, FileMoveLog, MediaItem, PlaybackStat,
    ServiceHeartbeat, TranscodeJob,
)
from media_manager.quality import calculate_quality_score
from media_manager.transcode.errors import MAX_ATTEMPTS, classify
from media_manager.transcode.estimate import estimate_output_bytes
from media_manager.web.services import transcode_planner as planner

logger = logging.getLogger(__name__)

LEASE_SECONDS = 180
WORKER_SERVICE = "transcoder"
WORKER_SILENT_AFTER_SECONDS = 600
# New arrivals are only auto-queued once their file has sat untouched this
# long, so the watcher's move + FileBot rename has finished.
AUTO_QUEUE_MIN_AGE_SECONDS = 3600

# Allowed write-ahead phase order (a phase may repeat, never go backwards).
PHASE_ORDER = ["queued", "preparing", "encoding", "verifying", "installing", "swapping", "swapped", "done"]


class JobError(Exception):
    """Raised for a request the job's current state doesn't allow (-> HTTP 409)."""


def _now() -> datetime:
    return datetime.utcnow()


def job_to_dict(job: TranscodeJob) -> Dict[str, Any]:
    return {
        "id": job.id, "media_item_id": job.media_item_id, "state": job.state, "phase": job.phase,
        "is_pilot": job.is_pilot, "attempts": job.attempts, "title": job.title, "media_type": job.media_type,
        "source_path": job.source_path, "source_size": job.source_size, "source_mtime": job.source_mtime,
        "source_codec": job.source_codec, "est_savings_bytes": job.est_savings_bytes,
        "plan": json.loads(job.plan_json) if job.plan_json else None,
        "progress_pct": job.progress_pct, "fps": job.fps, "eta_seconds": job.eta_seconds,
        "lease_owner": job.lease_owner,
        "lease_expires_at": job.lease_expires_at.isoformat() if job.lease_expires_at else None,
        "temp_path": job.temp_path, "backup_path": job.backup_path, "output_path": job.output_path,
        "output_size": job.output_size, "output_hash": job.output_hash,
        "saved_bytes": (job.source_size - job.output_size) if job.state == "done" and job.output_size else None,
        "verify": json.loads(job.verify_json) if job.verify_json else None,
        "error_code": job.error_code, "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


# ---------- enqueue ----------

async def _active_item_ids(db: AsyncSession) -> set:
    res = await db.execute(select(TranscodeJob.media_item_id).where(TranscodeJob.state.in_(TRANSCODE_ACTIVE_STATES)))
    return {row[0] for row in res.all()}


async def enqueue(db: AsyncSession, media_item_ids: List[str], pilot: bool) -> Dict[str, Any]:
    """Queues eligible items; returns created job ids and why others were skipped."""
    active = await _active_item_ids(db)
    created, skipped = [], []
    for item_id in dict.fromkeys(media_item_ids):
        evaluated = await planner.get_item_decision(db, item_id)
        if evaluated is None:
            skipped.append({"media_item_id": item_id, "reason": "not_found"})
            continue
        if item_id in active:
            skipped.append({"media_item_id": item_id, "reason": "already_queued"})
            continue
        item, decision = evaluated["item"], evaluated["decision"]
        if not decision.eligible:
            skipped.append({"media_item_id": item_id, "reason": decision.reason})
            continue
        created.append(_new_job(db, item, decision, pilot))
        active.add(item_id)
    await db.commit()
    return {"created": [j.id for j in created], "skipped": skipped}


def _new_job(db: AsyncSession, item: Dict[str, Any], decision, pilot: bool) -> TranscodeJob:
    try:
        mtime = os.path.getmtime(item["path"])
    except OSError:
        mtime = None
    out_bytes = estimate_output_bytes(item, decision.bucket)
    job = TranscodeJob(
        media_item_id=item["id"], state="pending", phase="queued", is_pilot=pilot,
        source_path=item["path"], source_size=item["size_bytes"] or 0, source_mtime=mtime,
        source_codec=item["video_codec"], title=item["title"], media_type=item["media_type"],
        est_savings_bytes=max(0, (item["size_bytes"] or 0) - out_bytes),
        plan_json=json.dumps(dict(decision.plan, bucket=decision.bucket)),
    )
    db.add(job)
    return job


async def enqueue_all(db: AsyncSession) -> Dict[str, Any]:
    """Queues every eligible, not-yet-queued file (full migration)."""
    active = await _active_item_ids(db)
    count = 0
    for entry in await planner._evaluate_all(db):
        item = entry["item"]
        if entry["decision"].eligible and item["id"] not in active:
            _new_job(db, item, entry["decision"], pilot=False)
            count += 1
    await db.commit()
    return {"created_count": count}


def _settled(path: str, cutoff: float) -> bool:
    try:
        return os.stat(path).st_ctime <= cutoff  # ctime moves on rename, unlike a preserved mtime
    except OSError:
        return False


async def enqueue_new(db: AsyncSession) -> Dict[str, Any]:
    """
    Full mode only: queues eligible files that have never had a job (new
    downloads). Unlike enqueue_all, files skipped or failed before are left
    alone, so a periodic run never retries them.
    """
    control = await planner.get_or_create_control(db)
    if control.mode != "full":
        return {"created_count": 0}
    seen = {row[0] for row in (await db.execute(
        select(TranscodeJob.media_item_id).where(TranscodeJob.media_item_id.isnot(None)))).all()}
    cutoff = time.time() - AUTO_QUEUE_MIN_AGE_SECONDS
    count = 0
    for entry in await planner._evaluate_all(db):
        item = entry["item"]
        if entry["decision"].eligible and item["id"] not in seen and _settled(item["path"], cutoff):
            _new_job(db, item, entry["decision"], pilot=False)
            count += 1
    await db.commit()
    if count:
        logger.info(f"[TRANSCODE] Auto-queued {count} new file(s)")
    return {"created_count": count}


# ---------- claiming / leases ----------

async def claim(db: AsyncSession, worker_id: str) -> Optional[TranscodeJob]:
    """
    Hands the best pending job to the worker (pilot jobs first, then biggest
    estimated savings). Pilot mode only releases pilot jobs. Each candidate is
    re-checked for eligibility (settings may have changed since enqueue).
    """
    control = await planner.get_or_create_control(db)
    if control.run_state != "running" or control.mode == "off":
        return None
    while True:
        query = select(TranscodeJob).where(TranscodeJob.state == "pending")
        if control.mode == "pilot":
            query = query.where(TranscodeJob.is_pilot.is_(True))
        query = query.order_by(TranscodeJob.is_pilot.desc(), TranscodeJob.est_savings_bytes.desc(),
                               TranscodeJob.created_at).limit(1)
        job = (await db.execute(query)).scalar_one_or_none()
        if job is None:
            return None
        evaluated = await planner.get_item_decision(db, job.media_item_id) if job.media_item_id else None
        if evaluated is None or not evaluated["decision"].eligible:
            job.state, job.error_code = "skipped", "not_eligible"
            job.error_message = evaluated["decision"].reason if evaluated else "media item no longer exists"
            job.finished_at = job.updated_at = _now()
            await db.commit()
            continue
        # Compare-and-set so two workers can never claim the same job.
        res = await db.execute(
            update(TranscodeJob)
            .where(TranscodeJob.id == job.id, TranscodeJob.state == "pending")
            .values(state="claimed", phase="preparing", lease_owner=worker_id,
                    lease_expires_at=_now() + timedelta(seconds=LEASE_SECONDS),
                    attempts=TranscodeJob.attempts + 1, started_at=_now(), updated_at=_now(),
                    plan_json=json.dumps(dict(evaluated["decision"].plan, bucket=evaluated["decision"].bucket)),
                    progress_pct=0.0, error_code=None, error_message=None)
        )
        await db.commit()
        if res.rowcount == 1:
            await db.refresh(job)
            return job


async def _owned_job(db: AsyncSession, job_id: str, worker_id: str) -> TranscodeJob:
    job = (await db.execute(select(TranscodeJob).where(TranscodeJob.id == job_id))).scalar_one_or_none()
    if job is None:
        raise JobError("Job not found")
    if job.lease_owner != worker_id:
        raise JobError("Job is leased to another worker")
    return job


async def _touch_worker(db: AsyncSession, worker_id: str) -> None:
    """
    Lease renewals prove the worker is alive. It only POSTs /heartbeat
    between jobs, so without this every job over WORKER_SILENT_AFTER_SECONDS
    showed the worker offline and fired a false "not responding" email.
    """
    row = (await db.execute(select(ServiceHeartbeat).where(ServiceHeartbeat.service == WORKER_SERVICE))).scalar_one_or_none()
    if row is not None and row.hostname == worker_id:
        row.last_beat_at = _now()


async def report_progress(db: AsyncSession, job_id: str, worker_id: str, progress_pct: Optional[float] = None,
                          fps: Optional[float] = None, eta_seconds: Optional[int] = None) -> Dict[str, Any]:
    """Renews the lease (and the worker heartbeat). Tells the worker to stop if the job was cancelled."""
    job = await _owned_job(db, job_id, worker_id)
    await _touch_worker(db, worker_id)
    if job.state == "cancelled":
        return {"cancel": True}
    if job.state not in ("claimed", "running", "needs_recovery"):
        raise JobError(f"Job is {job.state}")
    job.lease_expires_at = _now() + timedelta(seconds=LEASE_SECONDS)
    if job.state == "claimed":
        job.state = "running"
    if progress_pct is not None:
        job.progress_pct = max(0.0, min(100.0, float(progress_pct)))
    job.fps, job.eta_seconds, job.updated_at = fps, eta_seconds, _now()
    await db.commit()
    return {"cancel": False}


async def set_phase(db: AsyncSession, job_id: str, worker_id: str, phase: str,
                    paths: Optional[Dict[str, str]] = None) -> TranscodeJob:
    """
    Write-ahead: the worker records a phase (and its temp/backup/output
    paths) *before* doing it, so recovery knows exactly what may be on disk.
    """
    if phase not in PHASE_ORDER:
        raise JobError(f"Unknown phase '{phase}'")
    job = await _owned_job(db, job_id, worker_id)
    if job.state not in ("claimed", "running", "needs_recovery"):
        raise JobError(f"Job is {job.state}")
    if PHASE_ORDER.index(phase) < PHASE_ORDER.index(job.phase):
        raise JobError(f"Phase cannot go back from {job.phase} to {phase}")
    await _touch_worker(db, worker_id)
    if job.state == "claimed":
        job.state = "running"
    for key in ("temp_path", "backup_path", "output_path", "output_hash"):
        if paths and paths.get(key):
            setattr(job, key, paths[key])
    job.phase = phase
    job.lease_expires_at = _now() + timedelta(seconds=LEASE_SECONDS)
    job.updated_at = _now()
    await db.commit()
    return job


async def inflight(db: AsyncSession, worker_id: str) -> List[TranscodeJob]:
    """Jobs the worker must resolve first on startup: anything mid-swap, or still leased to it."""
    res = await db.execute(select(TranscodeJob).where(
        (TranscodeJob.state == "needs_recovery")
        | ((TranscodeJob.lease_owner == worker_id) & TranscodeJob.state.in_(("claimed", "running")))
    ))
    jobs = res.scalars().all()
    for job in jobs:
        # Take over recovery jobs so the following phase/complete calls are owned.
        job.lease_owner = worker_id
        job.lease_expires_at = _now() + timedelta(seconds=LEASE_SECONDS)
    await db.commit()
    return jobs


async def sweep_expired_leases(db: AsyncSession) -> Dict[str, int]:
    """
    A worker that stopped renewing its lease crashed or was cut off. Before
    the swap nothing on disk changed, so the job is simply retried; once a
    swap may have started it goes to needs_recovery for the worker to finish
    or roll back - never back into the queue.
    """
    res = await db.execute(select(TranscodeJob).where(
        TranscodeJob.state.in_(("claimed", "running")), TranscodeJob.lease_expires_at < _now()))
    counts = {"requeued": 0, "needs_recovery": 0, "skipped": 0}
    for job in res.scalars().all():
        if job.phase in TRANSCODE_SWAP_PHASES:
            job.state = "needs_recovery"
            counts["needs_recovery"] += 1
        elif job.attempts >= MAX_ATTEMPTS:
            job.state, job.error_code, job.finished_at = "skipped", "worker_lost", _now()
            job.error_message = "Worker stopped responding twice while converting this file."
            counts["skipped"] += 1
        else:
            job.state, job.phase, job.error_code = "pending", "queued", "worker_lost"
            counts["requeued"] += 1
        job.lease_owner = job.lease_expires_at = None
        job.updated_at = _now()
    await db.commit()
    if any(counts.values()):
        logger.warning(f"[TRANSCODE] Expired leases: {counts}")
    return counts


# ---------- completion / failure ----------

async def complete(db: AsyncSession, job_id: str, worker_id: str, payload: Dict[str, Any]) -> TranscodeJob:
    """
    The swap is done on disk: point the MediaItem row at the new file
    (same row, so its id, playback stats and history survive), record the
    move, and close the job. Idempotent - a resend after a crash is a no-op.
    """
    job = (await db.execute(select(TranscodeJob).where(TranscodeJob.id == job_id))).scalar_one_or_none()
    if job is None:
        raise JobError("Job not found")
    if job.state == "done":
        return job
    if job.lease_owner != worker_id:
        raise JobError("Job is leased to another worker")
    if job.state not in ("claimed", "running", "needs_recovery"):
        raise JobError(f"Job is {job.state}")

    output_path = payload["output_path"]
    probe = payload.get("probe") or {}
    item = (await db.execute(select(MediaItem).where(MediaItem.id == job.media_item_id))).scalar_one_or_none()
    quality_before = item.quality_score if item else 0
    quality_after = quality_before
    if item is not None:
        # A scan may have indexed the new file before this call: merge into the original row.
        stray = (await db.execute(select(MediaItem).where(MediaItem.path == output_path, MediaItem.id != item.id))).scalars().all()
        for row in stray:
            await db.execute(delete(PlaybackStat).where(PlaybackStat.media_item_id == row.id))
            await db.delete(row)
        await db.flush()
        meta = metadata_from_probe(probe, output_path) if probe else {}
        item.path = output_path
        if meta:
            item.resolution = meta["resolution_label"] or item.resolution
            item.height, item.width = meta["height"], meta["width"]
            item.video_codec = meta["video_codec"] or "hevc"
            item.audio_codec = meta["audio_codec"] or item.audio_codec
            item.audio_channels = meta["audio_channels"] or item.audio_channels
            item.bitrate = meta["bit_rate"]
            apply_probe_details(item, meta["details"])
        item.size_bytes = payload.get("output_size") or item.size_bytes
        try:
            item.file_created_at = datetime.fromtimestamp(os.path.getmtime(output_path))
        except OSError:
            pass
        item.quality_score = quality_after = calculate_quality_score(
            resolution=item.resolution, height=item.height, audio_codec=item.audio_codec,
            audio_channels=item.audio_channels, bitrate=item.bitrate, path=item.path,
            video_codec=item.video_codec, video_bitrate=item.video_bitrate,
        )
        item.last_scanned_at = _now()

    job.state, job.phase = "done", "done"
    job.output_path, job.output_size = output_path, payload.get("output_size")
    job.output_hash = payload.get("output_hash") or job.output_hash
    job.backup_path = payload.get("backup_path") or job.backup_path
    job.verify_json = json.dumps(payload.get("verify")) if payload.get("verify") is not None else job.verify_json
    job.progress_pct, job.finished_at, job.updated_at = 100.0, _now(), _now()
    job.lease_expires_at = None
    db.add(FileMoveLog(
        source_path=job.source_path, destination_path=output_path, media_title=job.title or "",
        quality_before=quality_before, quality_after=quality_after, copy_verified=True, status="TRANSCODED",
    ))
    await db.commit()
    _clear_caches()
    saved = (job.source_size or 0) - (job.output_size or 0)
    logger.info(f"[TRANSCODE_DONE] {job.title}: saved {saved / 1e9:.2f} GB ({job.source_path} -> {output_path})")
    return job


async def fail(db: AsyncSession, job_id: str, worker_id: str, error_code: str, message: str = "") -> TranscodeJob:
    """Retry once for transient errors, requeue for free on contention, else skip. Originals are untouched."""
    job = await _owned_job(db, job_id, worker_id)
    if job.state not in ("claimed", "running", "needs_recovery", "cancelled"):
        raise JobError(f"Job is {job.state}")
    action = classify(error_code)
    job.error_code, job.error_message = error_code, (message or "")[:4000]
    if job.state == "cancelled":
        pass  # the worker stopped because we told it to; keep the cancel
    elif action == "requeue":
        job.state, job.phase = "pending", "queued"
        job.attempts = max(0, job.attempts - 1)  # this try doesn't count
    elif action == "retry" and job.attempts < MAX_ATTEMPTS:
        job.state, job.phase = "pending", "queued"
    else:
        job.state = "skipped" if action == "skip" else "failed"
        job.finished_at = _now()
    job.lease_owner = job.lease_expires_at = None
    job.updated_at = _now()
    await db.commit()
    logger.info(f"[TRANSCODE_FAIL] {job.title}: {error_code} -> {job.state} ({message[:200]})")
    return job


def _clear_caches() -> None:
    from media_manager.web.api import cleanup, inventory, transcode
    inventory.clear_duplicates_cache()
    cleanup._clear_suggestions_cache()
    transcode._clear_dry_run_cache()


# ---------- admin actions / status ----------

async def requeue(db: AsyncSession, job_id: str) -> TranscodeJob:
    job = (await db.execute(select(TranscodeJob).where(TranscodeJob.id == job_id))).scalar_one_or_none()
    if job is None:
        raise JobError("Job not found")
    if job.state not in ("skipped", "failed", "cancelled"):
        raise JobError(f"Only skipped/failed/cancelled jobs can be requeued (job is {job.state})")
    if job.media_item_id in await _active_item_ids(db):
        raise JobError("This file already has an active job")
    job.state, job.phase, job.attempts = "pending", "queued", 0
    job.error_code = job.error_message = job.finished_at = None
    job.updated_at = _now()
    await db.commit()
    return job


async def cancel(db: AsyncSession, job_id: str) -> TranscodeJob:
    """Pending jobs stop immediately; a running encode is told to stop at its next progress report."""
    job = (await db.execute(select(TranscodeJob).where(TranscodeJob.id == job_id))).scalar_one_or_none()
    if job is None:
        raise JobError("Job not found")
    if job.phase in TRANSCODE_SWAP_PHASES or job.state == "needs_recovery":
        raise JobError("The file is mid-swap; it can't be cancelled until the swap finishes")
    if job.state not in ("pending", "claimed", "running"):
        raise JobError(f"Job is {job.state}")
    job.state = "cancelled"
    job.finished_at = job.updated_at = _now()
    await db.commit()
    return job


async def list_jobs(db: AsyncSession, state: Optional[str] = None, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    query = select(TranscodeJob)
    count_query = select(func.count(TranscodeJob.id))
    if state:
        query = query.where(TranscodeJob.state == state)
        count_query = count_query.where(TranscodeJob.state == state)
    query = query.order_by(TranscodeJob.updated_at.desc()).offset(offset).limit(limit)
    jobs = (await db.execute(query)).scalars().all()
    return {"total": (await db.execute(count_query)).scalar() or 0, "items": [job_to_dict(j) for j in jobs]}


async def get_job(db: AsyncSession, job_id: str) -> Optional[TranscodeJob]:
    return (await db.execute(select(TranscodeJob).where(TranscodeJob.id == job_id))).scalar_one_or_none()


async def record_heartbeat(db: AsyncSession, worker_id: str, version: Optional[str],
                           detail: Optional[Dict[str, Any]]) -> None:
    row = (await db.execute(select(ServiceHeartbeat).where(ServiceHeartbeat.service == WORKER_SERVICE))).scalar_one_or_none()
    if row is None:
        row = ServiceHeartbeat(service=WORKER_SERVICE)
        db.add(row)
    row.last_beat_at = _now()
    row.hostname = (worker_id or "")[:255]
    row.version = (version or "")[:32] or None
    row.detail_json = json.dumps(detail or {})
    if detail and detail.get("pid") is not None:
        try:
            row.pid = int(detail["pid"])
        except (TypeError, ValueError):
            pass
    await db.commit()


async def get_savings(db: AsyncSession) -> Dict[str, Any]:
    """Space saved by finished jobs (overall and per media type) and the estimate still queued."""
    by_type: Dict[str, Dict[str, int]] = {}
    for media_type, files, source, output in (await db.execute(
            select(TranscodeJob.media_type, func.count(TranscodeJob.id), func.sum(TranscodeJob.source_size),
                   func.sum(TranscodeJob.output_size))
            .where(TranscodeJob.state == "done").group_by(TranscodeJob.media_type))).all():
        entry = by_type.setdefault(media_type or "movie", {"files": 0, "source_bytes": 0, "output_bytes": 0})
        entry["files"] += files
        entry["source_bytes"] += int(source or 0)
        entry["output_bytes"] += int(output or 0)
    for entry in by_type.values():
        entry["saved_bytes"] = entry["source_bytes"] - entry["output_bytes"]
    files, remaining = (await db.execute(
        select(func.count(TranscodeJob.id), func.sum(TranscodeJob.est_savings_bytes))
        .where(TranscodeJob.state.in_(TRANSCODE_ACTIVE_STATES)))).one()
    total = {key: sum(e[key] for e in by_type.values())
             for key in ("files", "source_bytes", "output_bytes", "saved_bytes")}
    return {**total, "by_type": by_type, "remaining_files": files or 0,
            "remaining_estimated_bytes": int(remaining or 0)}


async def get_status(db: AsyncSession) -> Dict[str, Any]:
    from media_manager.config import VERSION
    control = planner.control_to_dict(await planner.get_or_create_control(db))
    counts = {state: n for state, n in (await db.execute(
        select(TranscodeJob.state, func.count(TranscodeJob.id)).group_by(TranscodeJob.state))).all()}
    savings = await get_savings(db)
    saved = savings["saved_bytes"]
    current = (await db.execute(select(TranscodeJob).where(TranscodeJob.state.in_(("claimed", "running", "needs_recovery")))
                                .order_by(TranscodeJob.started_at))).scalars().all()
    hb = (await db.execute(select(ServiceHeartbeat).where(ServiceHeartbeat.service == WORKER_SERVICE))).scalar_one_or_none()
    worker = None
    if hb is not None:
        age = (_now() - hb.last_beat_at).total_seconds()
        worker = {
            "worker_id": hb.hostname, "version": hb.version, "last_beat_at": hb.last_beat_at.isoformat(),
            "seconds_since_beat": int(age), "online": age < WORKER_SILENT_AFTER_SECONDS,
            "version_matches": hb.version == VERSION, "detail": json.loads(hb.detail_json or "{}"),
        }
    return {
        "control": control, "server_version": VERSION, "worker": worker,
        "counts": counts, "saved_bytes": int(saved), "savings": savings, "current": [job_to_dict(j) for j in current],
    }


def swap_locked_sync(session) -> Dict[str, set]:
    """
    For the sync scanner / duplicate purge: items whose file is mid-swap
    (its old path can briefly be missing, its new path briefly unindexed).
    """
    rows = session.query(TranscodeJob.media_item_id, TranscodeJob.output_path).filter(
        (TranscodeJob.state == "needs_recovery")
        | (TranscodeJob.state.in_(("claimed", "running")) & TranscodeJob.phase.in_(TRANSCODE_SWAP_PHASES))
    ).all()
    return {"item_ids": {r[0] for r in rows if r[0]}, "output_paths": {r[1] for r in rows if r[1]}}
