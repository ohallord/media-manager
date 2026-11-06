import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import select

from media_manager.database import AsyncSessionLocal
from media_manager.models import AppLogEvent, IntegrationConfig, MediaItem, PlaybackStat
from media_manager.playback_matching import match_media_item, normalize_path, parse_path_mappings
from media_manager.integrations.plex import PlexClient
from media_manager.integrations.emby import EmbyClient
from media_manager.integrations.jellyfin import JellyfinClient
# Imported as a module (not `from ... import SHARED_HTTP_CLIENT`) so this reads
# the pooled client web/app.py's lifespan assigns onto the module *after*
# import time - the same reason metadata_sync.py imports integrations this way.
from media_manager.web.api import integrations as integrations_module

logger = logging.getLogger(__name__)

SYNC_COMMIT_BATCH = 200

PLAYBACK_SOURCES = {
    "plex": PlexClient,
    "emby": EmbyClient,
    "jellyfin": JellyfinClient,
}


async def _load_source_config(db, service_name: str) -> Optional[IntegrationConfig]:
    res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name == service_name))
    return res.scalar_one_or_none()


async def _upsert_playback_stat(db, media_item_id: str, source: str, remote_item: Dict) -> None:
    res = await db.execute(
        select(PlaybackStat).where(
            PlaybackStat.media_item_id == media_item_id,
            PlaybackStat.source == source,
        )
    )
    stat = res.scalar_one_or_none()
    if stat is None:
        stat = PlaybackStat(media_item_id=media_item_id, source=source)
        db.add(stat)
    stat.source_item_id = remote_item.get("external_id")
    stat.play_count = remote_item.get("play_count") or 0
    stat.last_played_at = remote_item.get("last_played_at")
    stat.updated_at = datetime.utcnow()


async def run_playback_sync() -> Dict[str, Dict[str, object]]:
    """
    Pulls library play stats from every enabled Plex/Emby/Jellyfin source,
    matches each remote item to a local MediaItem (by path, with configured
    prefix-remapping and a unique-basename fallback - see
    media_manager/playback_matching.py), and upserts PlaybackStat rows.
    Returns a per-source {"matched", "unmatched", "error"} summary, used by
    the manual-trigger API endpoint and logged as a [PLAYBACK_SYNC] AppLogEvent
    per source for visibility (there's no UI for this yet).
    """
    summary: Dict[str, Dict[str, object]] = {}
    logger.info("Starting playback sync...")
    try:
        async with AsyncSessionLocal() as db:
            media_items = (await db.execute(select(MediaItem))).scalars().all()
            items_by_path: Dict[str, MediaItem] = {}
            items_by_basename: Dict[str, List[MediaItem]] = {}
            for item in media_items:
                items_by_path[os.path.normpath(item.path)] = item
                items_by_basename.setdefault(os.path.basename(item.path), []).append(item)

            for source_name, client_cls in PLAYBACK_SOURCES.items():
                config = await _load_source_config(db, source_name)
                if not config or not config.enabled or not config.api_url or not config.api_key_encrypted:
                    continue

                mappings = parse_path_mappings(config.extra_json)
                client = client_cls(
                    config.api_url,
                    config.api_key_encrypted,
                    http_client=integrations_module.SHARED_HTTP_CLIENT,
                )

                try:
                    remote_items = await client.get_library_items()
                except Exception as e:
                    logger.error(f"Playback sync: {source_name} fetch failed: {e}")
                    config.status = "ERROR"
                    config.last_checked_at = datetime.utcnow()
                    await db.commit()
                    summary[source_name] = {"matched": 0, "unmatched": 0, "error": True}
                    continue

                matched = 0
                unmatched = 0
                for remote_item in remote_items:
                    norm_path = normalize_path(remote_item.get("path", ""), mappings)
                    media_item = match_media_item(norm_path, items_by_path, items_by_basename)
                    if not media_item:
                        unmatched += 1
                        continue
                    matched += 1
                    await _upsert_playback_stat(db, media_item.id, source_name, remote_item)
                    if matched % SYNC_COMMIT_BATCH == 0:
                        # Short write transactions: SQLite has one writer, and a
                        # whole-library upsert held the lock long enough to stall
                        # every other request (worker heartbeats, UI writes).
                        await db.commit()

                config.status = "HEALTHY"
                config.last_checked_at = datetime.utcnow()
                message = f"Playback sync for {source_name}: {matched} matched, {unmatched} unmatched"
                logger.info(f"[PLAYBACK_SYNC] {message}")
                db.add(AppLogEvent(
                    level="INFO",
                    event_tag="[PLAYBACK_SYNC]",
                    message=message,
                    context_json=json.dumps({"source": source_name, "matched": matched, "unmatched": unmatched}),
                ))
                await db.commit()
                summary[source_name] = {"matched": matched, "unmatched": unmatched, "error": False}

        logger.info("Playback sync completed.")
    except Exception as e:
        logger.error(f"Error during playback sync: {e}", exc_info=True)
    return summary


async def playback_sync_scheduler():
    # A short startup delay (unlike metadata sync's 1h) since fresh play
    # counts are the core value of this feature, not a background nicety.
    await asyncio.sleep(300)
    interval_hours = int(os.getenv("PLAYBACK_SYNC_INTERVAL_HOURS", "6"))
    while True:
        await run_playback_sync()
        await asyncio.sleep(interval_hours * 3600)
