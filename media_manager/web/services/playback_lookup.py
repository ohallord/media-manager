"""
Shared read-side helpers for turning PlaybackStat rows into the merged
play-count/last-played view used everywhere play data is displayed:
web/api/playback.py's own /items and /series endpoints, plus
web/api/inventory.py (Inventory listing) and web/api/integrations.py
(title detail modal) - all three need the exact same "max across sources"
merge semantics, so it lives here once rather than being copied per caller.
"""
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.models import PlaybackStat


def isoformat(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt else None


async def load_stats_by_media_item(db: AsyncSession, media_item_ids: List[str]) -> Dict[str, List[PlaybackStat]]:
    if not media_item_ids:
        return {}
    result = await db.execute(select(PlaybackStat).where(PlaybackStat.media_item_id.in_(media_item_ids)))
    stats_by_item: Dict[str, List[PlaybackStat]] = {}
    for stat in result.scalars().all():
        stats_by_item.setdefault(stat.media_item_id, []).append(stat)
    return stats_by_item


def merge_stats(stats: List[PlaybackStat]) -> Tuple[int, Optional[datetime]]:
    """
    Merges per-source PlaybackStat rows for one MediaItem: max play_count and
    max last_played_at across sources, so the same physical watch tracked by
    two servers isn't double-counted (see PlaybackStat's docstring).
    """
    play_count = max((s.play_count for s in stats), default=0)
    last_played_at = max((s.last_played_at for s in stats if s.last_played_at), default=None)
    return play_count, last_played_at


def sources_with_plays(stats: List[PlaybackStat]) -> List[str]:
    """Distinct source names that recorded at least one play for this item."""
    return sorted({s.source for s in stats if s.play_count > 0})
