"""
Computes Library Cleanup Wizard suggestions: media nobody has watched in a
configurable number of days, grouped by media type. This only *suggests* -
nothing here deletes anything; deletion is a separate, explicit, admin-only
action in web/api/cleanup.py that takes a list of ids the user selected.

Defaults (never_played_grace_days=180, stale_after_days=365) follow the
common range used by comparable self-hosted tools (Maintainerr, Reclaimerr):
a grace period so freshly-added content isn't flagged before anyone's had a
chance to watch it, then a longer "abandoned" threshold for things watched
once and never revisited. Both are per-media-type and admin-configurable via
CleanupSettings, this module just supplies the defaults on first use.
"""
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.models import CleanupSettings, MediaItem
from media_manager.web.services.playback_lookup import isoformat, load_stats_by_media_item, merge_stats

DEFAULT_NEVER_PLAYED_GRACE_DAYS = 180
DEFAULT_STALE_AFTER_DAYS = 365


async def get_or_create_settings(db: AsyncSession, media_type: str) -> CleanupSettings:
    query = select(CleanupSettings).where(CleanupSettings.media_type == media_type)
    settings = (await db.execute(query)).scalar_one_or_none()
    if settings is None:
        settings = CleanupSettings(
            media_type=media_type,
            never_played_grace_days=DEFAULT_NEVER_PLAYED_GRACE_DAYS,
            stale_after_days=DEFAULT_STALE_AFTER_DAYS,
        )
        db.add(settings)
        try:
            await db.commit()
        except IntegrityError:
            # A concurrent first request created this media type's row first.
            await db.rollback()
            return (await db.execute(query)).scalar_one()
        await db.refresh(settings)
    return settings


def _suggestion_reason(play_count: int, last_played_at, added_at, now: datetime, settings: CleanupSettings):
    """Returns ('never_played'|'stale', None) if this item qualifies as a
    suggestion under the given settings, or (None, None) if it doesn't."""
    if play_count == 0:
        if added_at and (now - added_at).days >= settings.never_played_grace_days:
            return "never_played"
        return None
    if last_played_at and (now - last_played_at).days >= settings.stale_after_days:
        return "stale"
    return None


async def _movie_suggestions(db: AsyncSession, settings: CleanupSettings, now: datetime) -> Dict[str, Any]:
    items = (await db.execute(select(MediaItem).where(MediaItem.media_type == "movie"))).scalars().all()
    stats_by_item = await load_stats_by_media_item(db, [i.id for i in items])

    suggestions: List[Dict[str, Any]] = []
    for item in items:
        stats = stats_by_item.get(item.id)
        if not stats:
            continue  # never synced - no data, no suggestion
        play_count, last_played_at = merge_stats(stats)
        reason = _suggestion_reason(play_count, last_played_at, item.file_created_at, now, settings)
        if not reason:
            continue
        suggestions.append({
            "media_item_id": item.id,
            "title": item.title,
            "path": item.path,
            "size_bytes": item.size_bytes,
            "play_count": play_count,
            "last_played_at": isoformat(last_played_at),
            "added_at": isoformat(item.file_created_at),
            "reason": reason,
        })

    suggestions.sort(key=lambda s: s["size_bytes"], reverse=True)
    return {
        "never_played_grace_days": settings.never_played_grace_days,
        "stale_after_days": settings.stale_after_days,
        "suggestions": suggestions,
        "total_reclaimable_bytes": sum(s["size_bytes"] for s in suggestions),
    }


async def _tv_suggestions(db: AsyncSession, settings: CleanupSettings, now: datetime) -> Dict[str, Any]:
    from media_manager.web.api.inventory import get_tv_show_name

    items = (await db.execute(select(MediaItem).where(MediaItem.media_type == "tv"))).scalars().all()
    stats_by_item = await load_stats_by_media_item(db, [i.id for i in items])

    shows: Dict[str, List[MediaItem]] = {}
    for item in items:
        show_name = get_tv_show_name(item.path).strip() or item.title
        shows.setdefault(show_name, []).append(item)

    suggestions: List[Dict[str, Any]] = []
    for show_name, episodes in shows.items():
        if not all(stats_by_item.get(ep.id) for ep in episodes):
            continue  # not every episode has been synced - too risky to suggest the whole show

        total_plays = 0
        last_played_at = None
        added_at = None
        size_bytes = 0
        for ep in episodes:
            play_count, ep_last_played = merge_stats(stats_by_item.get(ep.id, []))
            total_plays += play_count
            size_bytes += ep.size_bytes or 0
            if ep_last_played and (not last_played_at or ep_last_played > last_played_at):
                last_played_at = ep_last_played
            if ep.file_created_at and (not added_at or ep.file_created_at < added_at):
                added_at = ep.file_created_at

        reason = _suggestion_reason(total_plays, last_played_at, added_at, now, settings)
        if not reason:
            continue
        suggestions.append({
            "show_name": show_name,
            "episode_count": len(episodes),
            "episode_media_item_ids": [ep.id for ep in episodes],
            "size_bytes": size_bytes,
            "play_count": total_plays,
            "last_played_at": isoformat(last_played_at),
            "added_at": isoformat(added_at),
            "reason": reason,
        })

    suggestions.sort(key=lambda s: s["size_bytes"], reverse=True)
    return {
        "never_played_grace_days": settings.never_played_grace_days,
        "stale_after_days": settings.stale_after_days,
        "suggestions": suggestions,
        "total_reclaimable_bytes": sum(s["size_bytes"] for s in suggestions),
    }


async def compute_suggestions(db: AsyncSession) -> Dict[str, Any]:
    now = datetime.utcnow()
    movie_settings = await get_or_create_settings(db, "movie")
    tv_settings = await get_or_create_settings(db, "tv")
    return {
        "movie": await _movie_suggestions(db, movie_settings, now),
        "tv": await _tv_suggestions(db, tv_settings, now),
    }
