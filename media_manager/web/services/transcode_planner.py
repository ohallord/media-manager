"""
Dry-run planning for the H.265 migration: settings/control persistence, and
the report of what would be converted and how much space it would save.
"""
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.config import SOURCE_DIRECTORIES
from media_manager.models import MediaItem, TranscodeControl, TranscodeSettings
from media_manager.transcode.estimate import estimate_output_bytes
from media_manager.transcode.profiles import DEFAULTS, SKIP_REASONS, decide

MEDIA_TYPES = ("movie", "tv")

# Realtime multiple a 1080p NVENC encode sustains on the RTX 5060 Ti at the
# default presets, measured on library samples - only used for the ETA.
ENCODE_SPEED_X_REALTIME = 6.0


def merge_settings(media_type: str, stored: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = dict(DEFAULTS[media_type])
    merged.update({k: v for k, v in (stored or {}).items() if k in merged})
    return merged


def validate_settings_update(media_type: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Only known keys, with the default's type; numbers must be >= 0. Raises ValueError."""
    defaults = DEFAULTS[media_type]
    clean: Dict[str, Any] = {}
    for key, value in updates.items():
        if key not in defaults:
            raise ValueError(f"Unknown setting '{key}'")
        expected = type(defaults[key])
        if expected is bool:
            if not isinstance(value, bool):
                raise ValueError(f"'{key}' must be true/false")
        elif expected is int:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"'{key}' must be a non-negative integer")
        elif not isinstance(value, expected):
            raise ValueError(f"'{key}' must be a {expected.__name__}")
        clean[key] = value
    if "preset" in clean and clean["preset"] not in {f"p{i}" for i in range(1, 8)}:
        raise ValueError("'preset' must be p1..p7")
    if "multipass" in clean and clean["multipass"] not in ("disabled", "qres", "fullres"):
        raise ValueError("'multipass' must be disabled, qres or fullres")
    for key in [k for k in clean if k.startswith("cq_")]:
        if not 0 <= clean[key] <= 51:
            raise ValueError(f"'{key}' must be 0-51")
    return clean


async def get_settings(db: AsyncSession, media_type: str) -> Dict[str, Any]:
    row = (await db.execute(select(TranscodeSettings).where(TranscodeSettings.media_type == media_type))).scalar_one_or_none()
    return merge_settings(media_type, json.loads(row.settings_json) if row else None)


async def update_settings(db: AsyncSession, media_type: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    clean = validate_settings_update(media_type, updates)
    row = (await db.execute(select(TranscodeSettings).where(TranscodeSettings.media_type == media_type))).scalar_one_or_none()
    if row is None:
        row = TranscodeSettings(media_type=media_type, settings_json="{}")
        db.add(row)
    stored = json.loads(row.settings_json or "{}")
    stored.update(clean)
    row.settings_json = json.dumps(stored, sort_keys=True)
    row.updated_at = datetime.utcnow()
    await db.commit()
    return merge_settings(media_type, stored)


async def get_or_create_control(db: AsyncSession) -> TranscodeControl:
    query = select(TranscodeControl).where(TranscodeControl.id == "global")
    row = (await db.execute(query)).scalar_one_or_none()
    if row is None:
        row = TranscodeControl(id="global")
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            # A concurrent first request (the H.265 page loads several at
            # once) created the singleton between our SELECT and INSERT.
            await db.rollback()
            return (await db.execute(query)).scalar_one()
        await db.refresh(row)
    return row


def control_to_dict(control: TranscodeControl) -> Dict[str, Any]:
    return {
        "run_state": control.run_state,
        "mode": control.mode,
        "source_codecs": [c for c in (control.source_codecs or "").split(",") if c],
        "excluded_prefixes": json.loads(control.excluded_prefixes_json or "[]"),
        "pause_on": control.pause_on or "transcode_only",
        "notify_emails": [e.strip() for e in (control.notify_emails or "").split(",") if e.strip()],
        "failure_streak_threshold": control.failure_streak_threshold or 3,
        "always_excluded_prefixes": watcher_source_prefixes(),
    }


def watcher_source_prefixes() -> List[str]:
    """Download/staging folders the watcher moves from (incl. Syncthing) are never touched."""
    return sorted({d["from_dir"] for d in SOURCE_DIRECTORIES if d.get("from_dir")})


def item_to_dict(item: MediaItem) -> Dict[str, Any]:
    return {
        "id": item.id, "path": item.path, "title": item.title, "media_type": item.media_type,
        "season": item.season, "episode": item.episode, "resolution": item.resolution,
        "height": item.height, "width": item.width, "video_codec": item.video_codec,
        "bitrate": item.bitrate, "video_bitrate": item.video_bitrate, "size_bytes": item.size_bytes,
        "duration_seconds": item.duration_seconds, "hdr_format": item.hdr_format,
        "bit_depth": item.bit_depth, "pix_fmt": item.pix_fmt, "field_order": item.field_order,
        "probe_version": item.probe_version,
    }


async def _evaluate_all(db: AsyncSession) -> List[Dict[str, Any]]:
    control = control_to_dict(await get_or_create_control(db))
    settings = {mt: await get_settings(db, mt) for mt in MEDIA_TYPES}
    excluded = control["excluded_prefixes"] + control["always_excluded_prefixes"]
    rows = (await db.execute(select(MediaItem))).scalars().all()
    evaluated = []
    for row in rows:
        item = item_to_dict(row)
        type_settings = settings.get(item["media_type"]) or settings["movie"]
        decision = decide(item, type_settings, control["source_codecs"], excluded)
        entry = {"item": item, "decision": decision}
        if decision.eligible:
            out_bytes = estimate_output_bytes(item, decision.bucket)
            entry["estimated_output_bytes"] = out_bytes
            entry["estimated_savings_bytes"] = max(0, (item["size_bytes"] or 0) - out_bytes)
        evaluated.append(entry)
    return evaluated


async def compute_dry_run_report(db: AsyncSession) -> Dict[str, Any]:
    evaluated = await _evaluate_all(db)
    by_reason: Dict[str, Dict[str, Any]] = {}
    by_bucket: Dict[tuple, Dict[str, Any]] = {}
    eligible = [e for e in evaluated if e["decision"].eligible]
    for entry in evaluated:
        item, decision = entry["item"], entry["decision"]
        if decision.eligible:
            key = (item["media_type"], decision.bucket, item["video_codec"])
            b = by_bucket.setdefault(key, {"media_type": key[0], "bucket": key[1], "video_codec": key[2],
                                           "count": 0, "bytes": 0, "estimated_savings_bytes": 0})
            b["count"] += 1
            b["bytes"] += item["size_bytes"] or 0
            b["estimated_savings_bytes"] += entry["estimated_savings_bytes"]
        else:
            r = by_reason.setdefault(decision.reason, {"reason": decision.reason,
                                                       "label": SKIP_REASONS.get(decision.reason, decision.reason),
                                                       "count": 0, "bytes": 0})
            r["count"] += 1
            r["bytes"] += item["size_bytes"] or 0

    eligible_bytes = sum(e["item"]["size_bytes"] or 0 for e in eligible)
    savings = sum(e["estimated_savings_bytes"] for e in eligible)
    encode_seconds = sum((e["item"]["duration_seconds"] or 0) for e in eligible) / ENCODE_SPEED_X_REALTIME
    return {
        "generated_at": datetime.utcnow().isoformat(),
        "totals": {
            "items": len(evaluated),
            "eligible": len(eligible),
            "eligible_bytes": eligible_bytes,
            "estimated_savings_bytes": savings,
            "estimated_output_bytes": eligible_bytes - savings,
            "estimated_encode_hours": round(encode_seconds / 3600, 1),
        },
        "by_reason": sorted(by_reason.values(), key=lambda r: -r["bytes"]),
        "by_bucket": sorted(by_bucket.values(), key=lambda b: -b["estimated_savings_bytes"]),
    }


async def list_candidates(db: AsyncSession, media_type: Optional[str] = None,
                          limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """Eligible items, biggest estimated savings first (the full-queue order)."""
    eligible = [e for e in await _evaluate_all(db) if e["decision"].eligible
                and (media_type is None or e["item"]["media_type"] == media_type)]
    eligible.sort(key=lambda e: -e["estimated_savings_bytes"])
    page = eligible[offset:offset + limit]
    return {
        "total": len(eligible),
        "items": [dict(e["item"], bucket=e["decision"].bucket, plan=e["decision"].plan,
                       estimated_output_bytes=e["estimated_output_bytes"],
                       estimated_savings_bytes=e["estimated_savings_bytes"]) for e in page],
    }


async def get_item_decision(db: AsyncSession, media_item_id: str) -> Optional[Dict[str, Any]]:
    row = (await db.execute(select(MediaItem).where(MediaItem.id == media_item_id))).scalar_one_or_none()
    if row is None:
        return None
    control = control_to_dict(await get_or_create_control(db))
    item = item_to_dict(row)
    settings = await get_settings(db, item["media_type"] if item["media_type"] in MEDIA_TYPES else "movie")
    decision = decide(item, settings, control["source_codecs"],
                      control["excluded_prefixes"] + control["always_excluded_prefixes"])
    return {"item": item, "decision": decision}
