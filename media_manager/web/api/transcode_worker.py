"""
API for the host H.265 worker (media-manager-transcoder.service). It holds
no DB of its own: it claims jobs, reports progress/phases and posts results
here, authenticated with the shared TRANSCODE_WORKER_TOKEN (k8s Secret on
this side, EnvironmentFile on the host).
"""
import hmac
import os
from types import SimpleNamespace
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.config import VERSION
from media_manager.database import get_async_db
from media_manager.transcode.protocol import PROTOCOL_VERSION, is_compatible
from media_manager.web.services import stream_activity
from media_manager.web.services import transcode_jobs as jobs
from media_manager.web.services import transcode_planner as planner
from media_manager.web.services.transcode_post_swap import notify_media_servers

router = APIRouter(prefix="/api/v1/transcode/worker", tags=["transcode-worker"])


def require_worker(
    x_transcode_token: str = Header(default="", alias="X-Transcode-Token"),
    x_transcode_protocol: str = Header(default="", alias="X-Transcode-Protocol"),
) -> None:
    expected = os.getenv("TRANSCODE_WORKER_TOKEN", "")
    if not expected:
        # No default token: a worker that can delete originals must be configured explicitly.
        raise HTTPException(status_code=503, detail="TRANSCODE_WORKER_TOKEN is not configured")
    try:
        ok = hmac.compare_digest(x_transcode_token or "", expected)
    except (TypeError, ValueError):
        ok = False
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid transcode worker token")
    if not is_compatible(x_transcode_protocol):
        raise HTTPException(status_code=409, detail=f"Worker protocol {x_transcode_protocol!r} unsupported; "
                                                    f"server speaks {PROTOCOL_VERSION}")


class HeartbeatRequest(BaseModel):
    worker_id: str
    version: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None


class WorkerRequest(BaseModel):
    worker_id: str


class ProgressRequest(WorkerRequest):
    progress_pct: Optional[float] = None
    fps: Optional[float] = None
    eta_seconds: Optional[int] = None


class PhaseRequest(WorkerRequest):
    phase: str
    temp_path: Optional[str] = None
    backup_path: Optional[str] = None
    output_path: Optional[str] = None
    output_hash: Optional[str] = None


class CompleteRequest(WorkerRequest):
    output_path: str
    output_size: int
    output_hash: Optional[str] = None
    backup_path: Optional[str] = None
    probe: Optional[Dict[str, Any]] = None
    verify: Optional[Dict[str, Any]] = None


class FailRequest(WorkerRequest):
    error_code: str
    message: str = ""


def _envelope(**extra) -> Dict[str, Any]:
    # Every response carries the server version: a worker seeing a new one
    # finishes its current job, exits, and systemd restarts it on new code.
    return {"server_version": VERSION, "protocol": PROTOCOL_VERSION, **extra}


def _conflict(e: jobs.JobError):
    raise HTTPException(status_code=409, detail=str(e))


async def _pause_state(db: AsyncSession, control, ignore_mode: bool = False) -> Dict[str, Any]:
    """Admin pause, or the media servers need the GPU (control.pause_on policy)."""
    if control.run_state != "running" or (control.mode == "off" and not ignore_mode):
        return {"pause": True, "reason": "paused by admin"}
    activity = await stream_activity.get_activity(db)
    reason = stream_activity.pause_reason(activity, control.pause_on or "transcode_only")
    return {"pause": bool(reason), "reason": reason or ""}


@router.post("/heartbeat", dependencies=[Depends(require_worker)])
async def worker_heartbeat(req: HeartbeatRequest, db: AsyncSession = Depends(get_async_db)):
    await jobs.record_heartbeat(db, req.worker_id, req.version, req.detail)
    control = await planner.get_or_create_control(db)
    return _envelope(**await _pause_state(db, control),
                     settings={mt: await planner.get_settings(db, mt) for mt in planner.MEDIA_TYPES})


@router.post("/claim", dependencies=[Depends(require_worker)])
async def worker_claim(req: WorkerRequest, db: AsyncSession = Depends(get_async_db)):
    job = await jobs.claim(db, req.worker_id)
    if job is None:
        return Response(status_code=204, headers={"X-Server-Version": VERSION})
    settings = await planner.get_settings(db, job.media_type if job.media_type in planner.MEDIA_TYPES else "movie")
    return _envelope(job=jobs.job_to_dict(job), settings=settings)


@router.get("/inflight", dependencies=[Depends(require_worker)])
async def worker_inflight(worker_id: str, db: AsyncSession = Depends(get_async_db)):
    return _envelope(jobs=[jobs.job_to_dict(j) for j in await jobs.inflight(db, worker_id)])


@router.post("/jobs/{job_id}/progress", dependencies=[Depends(require_worker)])
async def worker_progress(job_id: str, req: ProgressRequest, db: AsyncSession = Depends(get_async_db)):
    try:
        result = await jobs.report_progress(db, job_id, req.worker_id, req.progress_pct, req.fps, req.eta_seconds)
    except jobs.JobError as e:
        _conflict(e)
    control = await planner.get_or_create_control(db)
    # A running encode pauses for a Pause click or the media servers; switching
    # mode to 'off' only stops new claims, so it isn't a reason here.
    return _envelope(**await _pause_state(db, control, ignore_mode=True), **result)


@router.post("/jobs/{job_id}/phase", dependencies=[Depends(require_worker)])
async def worker_phase(job_id: str, req: PhaseRequest, db: AsyncSession = Depends(get_async_db)):
    if req.phase == "swapping":
        job = await jobs.get_job(db, job_id)
        if job is not None and stream_activity.is_playing(await stream_activity.get_activity(db, force=True), job.source_path):
            # 423: never pull a file out from under someone watching it - the worker waits and retries.
            raise HTTPException(status_code=423, detail="File is being played right now")
    try:
        job = await jobs.set_phase(db, job_id, req.worker_id, req.phase,
                                   {"temp_path": req.temp_path, "backup_path": req.backup_path,
                                    "output_path": req.output_path, "output_hash": req.output_hash})
    except jobs.JobError as e:
        _conflict(e)
    return _envelope(job=jobs.job_to_dict(job))


@router.post("/jobs/{job_id}/complete", dependencies=[Depends(require_worker)])
async def worker_complete(job_id: str, req: CompleteRequest, background: BackgroundTasks,
                          db: AsyncSession = Depends(get_async_db)):
    try:
        already_done = (await jobs.get_job(db, job_id) or SimpleNamespace(state=None)).state == "done"
        job = await jobs.complete(db, job_id, req.worker_id, req.model_dump())
    except jobs.JobError as e:
        _conflict(e)
    if not already_done:
        background.add_task(notify_media_servers, job.source_path, job.output_path)
    return _envelope(job=jobs.job_to_dict(job))


@router.post("/jobs/{job_id}/fail", dependencies=[Depends(require_worker)])
async def worker_fail(job_id: str, req: FailRequest, db: AsyncSession = Depends(get_async_db)):
    try:
        job = await jobs.fail(db, job_id, req.worker_id, req.error_code, req.message)
    except jobs.JobError as e:
        _conflict(e)
    return _envelope(job=jobs.job_to_dict(job))
