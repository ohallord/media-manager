"""
What Plex / Jellyfin / Emby are playing right now - drives the transcode
worker's pause decision and the "never swap a file someone is watching" rule.
Polled on demand and cached briefly (the worker heartbeats every ~15 s).
"""
import logging
import os
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from media_manager.integrations.emby import EmbyClient
from media_manager.integrations.jellyfin import JellyfinClient
from media_manager.integrations.plex import PlexClient
from media_manager.models import IntegrationConfig
from media_manager.playback_matching import normalize_path, parse_path_mappings

logger = logging.getLogger(__name__)

SOURCES = {"plex": PlexClient, "jellyfin": JellyfinClient, "emby": EmbyClient}
CACHE_TTL_SECONDS = 30
PAUSE_POLICIES = ("transcode_only", "any_stream", "never")

_cache: Dict[str, Any] = {}


def clear_cache() -> None:
    _cache.clear()


async def _configs(db: AsyncSession) -> List[IntegrationConfig]:
    res = await db.execute(select(IntegrationConfig).where(IntegrationConfig.service_name.in_(SOURCES)))
    return [c for c in res.scalars().all() if c.enabled and c.api_url and c.api_key_encrypted]


async def get_activity(db: AsyncSession, force: bool = False) -> Dict[str, Any]:
    """{"sessions": [{"source", "path", "video_transcode", "state"}], "video_transcodes", "streams", "errors"}."""
    now = time.time()
    if not force and _cache and now - _cache["at"] < CACHE_TTL_SECONDS:
        return _cache["activity"]
    from media_manager.web.api import integrations as integrations_module
    sessions: List[Dict[str, Any]] = []
    errors: List[str] = []
    for config in await _configs(db):
        client = SOURCES[config.service_name](config.api_url, config.api_key_encrypted,
                                              http_client=integrations_module.SHARED_HTTP_CLIENT)
        mappings = parse_path_mappings(config.extra_json)
        try:
            for s in await client.get_sessions():
                path = normalize_path(s["path"], mappings) if s.get("path") else None
                sessions.append({"source": config.service_name, "path": os.path.normpath(path) if path else None,
                                 "video_transcode": bool(s.get("video_transcode")), "state": s.get("state")})
        except Exception as e:  # one server being down must not stall the queue
            logger.warning(f"[TRANSCODE] {config.service_name} sessions unavailable: {e}")
            errors.append(config.service_name)
    activity = {
        "sessions": sessions,
        "streams": len(sessions),
        "video_transcodes": sum(1 for s in sessions if s["video_transcode"]),
        "errors": errors,
    }
    _cache.update({"at": now, "activity": activity})
    return activity


def pause_reason(activity: Dict[str, Any], policy: str) -> Optional[str]:
    """Why the worker should yield the GPU right now, or None."""
    if policy == "never":
        return None
    if policy == "any_stream" and activity["streams"]:
        return f"{activity['streams']} stream(s) playing"
    if activity["video_transcodes"]:
        return f"{activity['video_transcodes']} media-server video transcode(s) using the GPU"
    return None


def is_playing(activity: Dict[str, Any], path: str) -> bool:
    target = os.path.normpath(path)
    return any(s["path"] == target for s in activity["sessions"] if s.get("path"))
