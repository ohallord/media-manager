import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.database import get_async_db
from media_manager.metadata import PROBE_VERSION, run_ffprobe
from media_manager.models import User
from media_manager.transcode.ffmpeg_args import build_encode_args, stream_plan
from media_manager.transcode.profiles import SKIP_REASONS
from media_manager.web.api.auth import get_admin_user, get_current_active_user
from media_manager.web.services import media_probe_backfill, transcode_jobs, transcode_planner

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/transcode",
    tags=["transcode"],
    # Admin-only: cleanup/migration expose library paths and destructive actions.
    dependencies=[Depends(get_admin_user)],
)


@router.get("/probe-backfill")
async def get_probe_backfill_status(user: User = Depends(get_current_active_user)):
    """Progress of the current/last rich-metadata backfill plus DB coverage."""
    return {
        "probe_version": PROBE_VERSION,
        "running": media_probe_backfill.is_running(),
        "status": media_probe_backfill.get_status(),
        "coverage": await media_probe_backfill.get_coverage(),
    }


@router.post("/probe-backfill", status_code=202)
async def start_probe_backfill(admin: User = Depends(get_admin_user)):
    """Re-probes every row missing rich codec metadata (admin only)."""
    if not media_probe_backfill.start_probe_backfill():
        raise HTTPException(status_code=409, detail="A probe backfill is already running.")
    logger.info("[PROBE_BACKFILL] Backfill started by admin.")
    return {"status": "started"}


@router.post("/rescore")
async def rescore_media_items(admin: User = Depends(get_admin_user)):
    """Recomputes every row's quality_score from stored fields (admin only)."""
    return await media_probe_backfill.rescore_all()


# The report walks every media_items row; cache it briefly like the cleanup
# suggestions, and drop the cache whenever settings change.
DRY_RUN_CACHE_TTL_SECONDS = 300
_dry_run_cache: Dict[str, Any] = {}


def _clear_dry_run_cache() -> None:
    _dry_run_cache.clear()


class ControlUpdateRequest(BaseModel):
    source_codecs: Optional[List[str]] = None
    excluded_prefixes: Optional[List[str]] = None
    run_state: Optional[str] = None
    mode: Optional[str] = None
    pause_on: Optional[str] = None
    notify_emails: Optional[List[str]] = None
    failure_streak_threshold: Optional[int] = None


class EnqueueRequest(BaseModel):
    media_item_ids: List[str]
    pilot: bool = True


class EnqueueAllRequest(BaseModel):
    confirm: bool = False


@router.get("/settings")
async def get_transcode_settings(db: AsyncSession = Depends(get_async_db),
                                 user: User = Depends(get_current_active_user)):
    return {mt: await transcode_planner.get_settings(db, mt) for mt in transcode_planner.MEDIA_TYPES}


@router.put("/settings/{media_type}")
async def update_transcode_settings(media_type: str, updates: Dict[str, Any],
                                    db: AsyncSession = Depends(get_async_db),
                                    admin: User = Depends(get_admin_user)):
    if media_type not in transcode_planner.MEDIA_TYPES:
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tv'")
    try:
        settings = await transcode_planner.update_settings(db, media_type, updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _clear_dry_run_cache()
    return settings


@router.get("/control")
async def get_transcode_control(db: AsyncSession = Depends(get_async_db),
                                user: User = Depends(get_current_active_user)):
    return transcode_planner.control_to_dict(await transcode_planner.get_or_create_control(db))


@router.put("/control")
async def update_transcode_control(req: ControlUpdateRequest, db: AsyncSession = Depends(get_async_db),
                                   admin: User = Depends(get_admin_user)):
    control = await transcode_planner.get_or_create_control(db)
    if req.source_codecs is not None:
        codecs = [c.strip().lower() for c in req.source_codecs if c and c.strip()]
        if "hevc" in codecs:
            raise HTTPException(status_code=400, detail="hevc is the target codec, not a source")
        control.source_codecs = ",".join(dict.fromkeys(codecs))
    if req.excluded_prefixes is not None:
        prefixes = [p.strip() for p in req.excluded_prefixes if p and p.strip()]
        if any(not p.startswith("/") for p in prefixes):
            raise HTTPException(status_code=400, detail="Excluded folders must be absolute paths")
        control.excluded_prefixes_json = json.dumps(prefixes)
    if req.run_state is not None:
        if req.run_state not in ("running", "paused"):
            raise HTTPException(status_code=400, detail="run_state must be 'running' or 'paused'")
        control.run_state = req.run_state
    if req.mode is not None:
        if req.mode not in ("off", "pilot", "full"):
            raise HTTPException(status_code=400, detail="mode must be 'off', 'pilot' or 'full'")
        control.mode = req.mode
    if req.pause_on is not None:
        from media_manager.web.services.stream_activity import PAUSE_POLICIES
        if req.pause_on not in PAUSE_POLICIES:
            raise HTTPException(status_code=400, detail=f"pause_on must be one of {', '.join(PAUSE_POLICIES)}")
        control.pause_on = req.pause_on
    if req.notify_emails is not None:
        emails = [e.strip() for e in req.notify_emails if e and e.strip()]
        if any("@" not in e or "," in e for e in emails):
            raise HTTPException(status_code=400, detail="notify_emails must be email addresses")
        control.notify_emails = ",".join(emails)
    if req.failure_streak_threshold is not None:
        if not 1 <= req.failure_streak_threshold <= 50:
            raise HTTPException(status_code=400, detail="failure_streak_threshold must be 1-50")
        control.failure_streak_threshold = req.failure_streak_threshold
    await db.commit()
    _clear_dry_run_cache()
    return transcode_planner.control_to_dict(control)


@router.get("/dry-run")
async def get_dry_run_report(force_refresh: bool = False, db: AsyncSession = Depends(get_async_db),
                             user: User = Depends(get_current_active_user)):
    """What the migration would convert/skip and the projected space saved."""
    now = time.time()
    if not force_refresh and _dry_run_cache and now - _dry_run_cache["at"] < DRY_RUN_CACHE_TTL_SECONDS:
        return _dry_run_cache["report"]
    report = await transcode_planner.compute_dry_run_report(db)
    _dry_run_cache.update({"at": now, "report": report})
    return report


@router.get("/candidates")
async def get_transcode_candidates(media_type: Optional[str] = None,
                                   limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                                   db: AsyncSession = Depends(get_async_db),
                                   user: User = Depends(get_current_active_user)):
    return await transcode_planner.list_candidates(db, media_type, limit, offset)


@router.get("/preview-command/{media_item_id}")
async def preview_transcode_command(media_item_id: str, db: AsyncSession = Depends(get_async_db),
                                    admin: User = Depends(get_admin_user)):
    """The exact ffmpeg command the worker would run for one file (re-probed live)."""
    evaluated = await transcode_planner.get_item_decision(db, media_item_id)
    if evaluated is None:
        raise HTTPException(status_code=404, detail="Media item not found")
    item, decision = evaluated["item"], evaluated["decision"]
    if not decision.eligible:
        return {"eligible": False, "reason": decision.reason,
                "reason_label": SKIP_REASONS.get(decision.reason, decision.reason)}
    try:
        probe = await asyncio.get_running_loop().run_in_executor(None, run_ffprobe, item["path"])
    except Exception as e:
        raise HTTPException(status_code=409, detail=f"Could not probe file: {e}")
    output = os.path.splitext(item["path"])[0] + ".mkv"
    args = build_encode_args(probe, decision.plan, item["path"], output + ".mm-partial")
    return {"eligible": True, "plan": decision.plan, "bucket": decision.bucket,
            "output_path": output, "dropped_streams": stream_plan(probe)["dropped"], "command": args}


def _job_conflict(e: "transcode_jobs.JobError"):
    raise HTTPException(status_code=409, detail=str(e))


@router.get("/status")
async def get_transcode_status(db: AsyncSession = Depends(get_async_db),
                               user: User = Depends(get_current_active_user)):
    """Worker liveness/version, current job, queue counts, space saved, media-server activity."""
    from media_manager.web.services import stream_activity
    status = await transcode_jobs.get_status(db)
    activity = await stream_activity.get_activity(db)
    from media_manager.web.services.transcode_notify import retained_backups
    status["retained_originals"] = await retained_backups(db)
    # Until a kept original is purged, its whole size is still on disk.
    status["savings"]["freed_bytes"] = status["savings"]["saved_bytes"] - status["retained_originals"]["bytes"]
    status["streams"] = {
        "streams": activity["streams"], "video_transcodes": activity["video_transcodes"],
        "errors": activity["errors"],
        "pause_reason": stream_activity.pause_reason(activity, status["control"]["pause_on"]),
    }
    return status


@router.post("/jobs")
async def enqueue_transcode_jobs(req: EnqueueRequest, db: AsyncSession = Depends(get_async_db),
                                 admin: User = Depends(get_admin_user)):
    """Queues specific files - the pilot (default) or hand-picked extras."""
    if not req.media_item_ids:
        raise HTTPException(status_code=400, detail="media_item_ids is empty")
    return await transcode_jobs.enqueue(db, req.media_item_ids, req.pilot)


@router.post("/jobs/enqueue-all")
async def enqueue_all_transcode_jobs(req: EnqueueAllRequest, db: AsyncSession = Depends(get_async_db),
                                     admin: User = Depends(get_admin_user)):
    """Queues every eligible file (full migration). Requires confirm=true."""
    if not req.confirm:
        raise HTTPException(status_code=400, detail="Pass confirm=true to queue the whole library")
    return await transcode_jobs.enqueue_all(db)


@router.get("/jobs")
async def list_transcode_jobs(state: Optional[str] = None, limit: int = Query(50, ge=1, le=500),
                              offset: int = Query(0, ge=0), db: AsyncSession = Depends(get_async_db),
                              user: User = Depends(get_current_active_user)):
    return await transcode_jobs.list_jobs(db, state, limit, offset)


@router.get("/jobs/{job_id}")
async def get_transcode_job(job_id: str, db: AsyncSession = Depends(get_async_db),
                            user: User = Depends(get_current_active_user)):
    job = await transcode_jobs.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return transcode_jobs.job_to_dict(job)


@router.post("/jobs/{job_id}/requeue")
async def requeue_transcode_job(job_id: str, db: AsyncSession = Depends(get_async_db),
                                admin: User = Depends(get_admin_user)):
    try:
        return transcode_jobs.job_to_dict(await transcode_jobs.requeue(db, job_id))
    except transcode_jobs.JobError as e:
        _job_conflict(e)


@router.post("/jobs/{job_id}/cancel")
async def cancel_transcode_job(job_id: str, db: AsyncSession = Depends(get_async_db),
                               admin: User = Depends(get_admin_user)):
    try:
        return transcode_jobs.job_to_dict(await transcode_jobs.cancel(db, job_id))
    except transcode_jobs.JobError as e:
        _job_conflict(e)
