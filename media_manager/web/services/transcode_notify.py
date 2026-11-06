"""
H.265 migration housekeeping that runs in the web pod's scheduler:
  - emails: a daily summary, an alert when several jobs in a row fail, and an
    alert (once per outage) when the worker stops checking in while the queue
    is supposed to be running, and one when the worker reports the GPU
    unusable (e.g. NVIDIA driver/library mismatch: needs a reboot);
  - purging the kept originals (.mm-orig) once their retention has passed.
"""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.config import MEDIA_DIRECTORIES
from media_manager.models import ServiceHeartbeat, TranscodeControl, TranscodeJob, User
from media_manager.quality import volume_is_healthy
from media_manager.transcode import fsops
from media_manager.web.services import transcode_planner as planner
from media_manager.web.services.transcode_jobs import WORKER_SERVICE, WORKER_SILENT_AFTER_SECONDS

logger = logging.getLogger(__name__)

DAILY_SUMMARY_INTERVAL = timedelta(hours=24)
# Finished jobs that point at a real problem (not just "this file wasn't worth it").
PROBLEM_CODES = {"verify_failed", "needs_manual_review", "worker_lost", "encoder_crash", "copy_failed",
                 "hash_mismatch", "source_corrupt", "permission_denied"}
GB = 1e9


def _now() -> datetime:
    return datetime.utcnow()


async def recipients(db: AsyncSession, control: TranscodeControl) -> List[str]:
    configured = [e.strip() for e in (control.notify_emails or "").split(",") if e.strip()]
    if configured:
        return configured
    res = await db.execute(select(User.email).where(User.role == "admin", User.is_active.is_(True), User.email.isnot(None)))
    return [e for e in (row[0] for row in res.all()) if e]


async def _send(db: AsyncSession, control: TranscodeControl, subject: str, body: str) -> int:
    from media_manager.web.services.email import send_email_notification
    sent = 0
    for to in await recipients(db, control):
        try:
            if await send_email_notification(to, subject, body):
                sent += 1
        except Exception as e:
            logger.warning(f"[TRANSCODE_NOTIFY] Email to {to} failed: {e}")
    return sent


# ---------- daily summary ----------

async def build_daily_summary(db: AsyncSession, since: datetime) -> Tuple[str, str, bool]:
    """(subject, body, anything_happened)."""
    window = await db.execute(
        select(TranscodeJob.state, func.count(TranscodeJob.id),
               func.sum(TranscodeJob.source_size - TranscodeJob.output_size))
        .where(TranscodeJob.finished_at >= since).group_by(TranscodeJob.state))
    by_state = {state: (count, saved or 0) for state, count, saved in window.all()}
    done, saved_window = by_state.get("done", (0, 0))
    total_saved = (await db.execute(select(func.sum(TranscodeJob.source_size - TranscodeJob.output_size))
                                    .where(TranscodeJob.state == "done"))).scalar() or 0
    pending = (await db.execute(select(func.count(TranscodeJob.id)).where(TranscodeJob.state == "pending"))).scalar() or 0
    problems = (await db.execute(select(TranscodeJob.title, TranscodeJob.error_code).where(
        TranscodeJob.finished_at >= since, TranscodeJob.state.in_(("failed", "skipped")))
        .order_by(TranscodeJob.finished_at.desc()).limit(15))).all()
    lines = [
        f"Last 24 hours: {done} file(s) converted, {saved_window / GB:.1f} GB saved.",
        f"Total saved so far: {total_saved / GB:.1f} GB.",
        f"Still queued: {pending} file(s).",
    ]
    skipped = by_state.get("skipped", (0, 0))[0]
    failed = by_state.get("failed", (0, 0))[0]
    if skipped or failed:
        lines.append(f"Skipped: {skipped}, failed: {failed} (originals kept):")
        lines += [f"  - {title}: {code}" for title, code in problems]
    happened = bool(done or skipped or failed or pending)
    subject = f"H.265 migration: {done} converted, {saved_window / GB:.1f} GB saved"
    return subject, "\n".join(lines) + "\n", happened


async def maybe_send_daily_summary(db: AsyncSession, control: TranscodeControl) -> bool:
    now = _now()
    if control.last_daily_summary_at and now - control.last_daily_summary_at < DAILY_SUMMARY_INTERVAL:
        return False
    since = control.last_daily_summary_at or now - DAILY_SUMMARY_INTERVAL
    subject, body, happened = await build_daily_summary(db, since)
    control.last_daily_summary_at = now
    await db.commit()
    if not happened or control.mode == "off":
        return False
    return await _send(db, control, subject, body) > 0


# ---------- failure streak ----------

def _is_problem(job: TranscodeJob) -> bool:
    return job.state == "failed" or (job.state == "skipped" and job.error_code in PROBLEM_CODES)


async def maybe_alert_failure_streak(db: AsyncSession, control: TranscodeControl) -> bool:
    threshold = control.failure_streak_threshold or 3
    recent = (await db.execute(select(TranscodeJob).where(TranscodeJob.state.in_(("done", "skipped", "failed")))
                               .order_by(TranscodeJob.finished_at.desc()).limit(threshold))).scalars().all()
    if len(recent) < threshold or not all(_is_problem(j) for j in recent):
        return False
    if control.last_streak_alert_job_id == recent[0].id:
        return False  # already told them about this streak
    control.last_streak_alert_job_id = recent[0].id
    await db.commit()
    body = (f"The last {threshold} H.265 conversions all failed - something may be wrong with the worker, "
            f"GPU or storage. Originals were kept.\n\n"
            + "\n".join(f"  - {j.title}: {j.error_code} {(j.error_message or '')[:200]}" for j in recent)
            + "\n\nThe queue is still running; pause it on the H.265 Migration page if needed.\n")
    return await _send(db, control, f"H.265 migration: {threshold} failures in a row", body) > 0


# ---------- worker silent ----------

async def maybe_alert_worker_silent(db: AsyncSession, control: TranscodeControl) -> bool:
    if control.run_state != "running" or control.mode == "off":
        return False
    hb = (await db.execute(select(ServiceHeartbeat).where(ServiceHeartbeat.service == WORKER_SERVICE))).scalar_one_or_none()
    last_beat = hb.last_beat_at if hb else None
    if last_beat and (_now() - last_beat).total_seconds() < WORKER_SILENT_AFTER_SECONDS:
        return False
    # Once per outage: only re-alert if the worker came back after the last alert.
    if control.last_silent_alert_at and (last_beat is None or last_beat <= control.last_silent_alert_at):
        return False
    control.last_silent_alert_at = _now()
    await db.commit()
    seen = f"last seen {last_beat.isoformat()} UTC" if last_beat else "has never checked in"
    body = (f"The H.265 queue is running but the transcode worker {seen}.\n\n"
            "On the media server: systemctl status media-manager-transcoder; journalctl -u media-manager-transcoder -n 50\n")
    return await _send(db, control, "H.265 migration: transcode worker is not responding", body) > 0


# ---------- GPU unavailable ----------

async def maybe_alert_gpu_unavailable(db: AsyncSession, control: TranscodeControl) -> bool:
    """Once per outage, when a live worker says nvidia-smi fails (it waits instead of encoding)."""
    if control.mode == "off":
        return False
    hb = (await db.execute(select(ServiceHeartbeat).where(ServiceHeartbeat.service == WORKER_SERVICE))).scalar_one_or_none()
    if hb is None or (_now() - hb.last_beat_at).total_seconds() >= WORKER_SILENT_AFTER_SECONDS:
        return False  # a silent worker is maybe_alert_worker_silent's job
    try:
        detail = json.loads(hb.detail_json or "{}")
    except ValueError:
        detail = {}
    error = detail.get("gpu_error")
    if not error:
        if control.last_gpu_alert_at is not None:
            control.last_gpu_alert_at = None  # recovered: the next outage alerts again
            await db.commit()
        return False
    if control.last_gpu_alert_at is not None:
        return False
    control.last_gpu_alert_at = _now()
    await db.commit()
    if detail.get("auto_reboot"):
        fix = ("The nightly check (media-manager-gpu-reboot.timer) will reboot the media server at 03:30 local "
               "time if this is a driver/library version mismatch. To fix it sooner: sudo reboot")
    else:
        fix = ("A driver/library version mismatch (unattended NVIDIA upgrade) needs a reboot: sudo reboot\n"
               "To have this fixed automatically at 03:30 local time next time, on the media server run:\n"
               "  sudo bash scripts/install_gpu_reboot_timer.sh")
    body = (f"The H.265 transcode worker can't use the GPU, so the migration is waiting:\n\n  {error}\n\n"
            f"{fix}\n\nRunbook: docs/runbooks/nvidia-driver-mismatch.md. Plex/Jellyfin hardware "
            "transcoding is probably affected too.\n")
    return await _send(db, control, "H.265 migration: GPU unavailable (reboot needed?)", body) > 0


# ---------- kept originals ----------

def _volume_for(path: str) -> str:
    best = ""
    for root in MEDIA_DIRECTORIES:
        root = root.rstrip("/")
        if root and path.startswith(root + "/") and len(root) > len(best):
            best = root
    return best or os.path.dirname(path)


async def purge_expired_backups(db: AsyncSession) -> Dict[str, int]:
    """Deletes .mm-orig originals whose retention has passed (only on healthy volumes)."""
    settings = {mt: await planner.get_settings(db, mt) for mt in planner.MEDIA_TYPES}
    jobs = (await db.execute(select(TranscodeJob).where(
        TranscodeJob.state == "done", TranscodeJob.backup_path.isnot(None),
        TranscodeJob.backup_purged_at.is_(None)))).scalars().all()
    counts = {"purged": 0, "bytes": 0, "skipped_unhealthy": 0, "missing": 0}
    now = _now()
    for job in jobs:
        days = settings.get(job.media_type or "movie", settings["movie"]).get("retain_originals_days", 7)
        if not job.finished_at or now < job.finished_at + timedelta(days=days):
            continue
        if not volume_is_healthy(_volume_for(job.backup_path)):
            counts["skipped_unhealthy"] += 1
            continue
        path = fsops.find_backup(job.backup_path)
        if path is None:
            # Already gone (deleted by hand); say so rather than claim a purge.
            logger.warning(f"[TRANSCODE] Kept original not found, marking purged: {job.backup_path}")
            counts["missing"] += 1
            job.backup_purged_at = now
            continue
        try:
            size = os.path.getsize(path)
            os.remove(path)
            counts["purged"] += 1
            counts["bytes"] += size
        except FileNotFoundError:
            counts["missing"] += 1
        except OSError as e:
            logger.warning(f"[TRANSCODE] Could not purge {path}: {e}")
            continue
        job.backup_purged_at = now
    await db.commit()
    if counts["purged"]:
        logger.info(f"[TRANSCODE] Purged {counts['purged']} kept original(s), {counts['bytes'] / GB:.1f} GB freed")
    return counts


async def retained_backups(db: AsyncSession) -> Dict[str, int]:
    row = (await db.execute(select(func.count(TranscodeJob.id), func.sum(TranscodeJob.source_size)).where(
        TranscodeJob.state == "done", TranscodeJob.backup_path.isnot(None),
        TranscodeJob.backup_purged_at.is_(None)))).one()
    return {"count": row[0] or 0, "bytes": int(row[1] or 0)}


async def run_notifications(db: AsyncSession) -> None:
    control = await planner.get_or_create_control(db)
    for check in (maybe_alert_worker_silent, maybe_alert_gpu_unavailable, maybe_alert_failure_streak,
                  maybe_send_daily_summary):
        try:
            await check(db, control)
        except Exception as e:
            logger.error(f"[TRANSCODE_NOTIFY] {check.__name__} failed: {e}", exc_info=True)
            await db.rollback()
