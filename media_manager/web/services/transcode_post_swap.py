"""
After a file is swapped for its HEVC version, tell the media servers so they
re-probe it now instead of at their next scheduled scan.

Radarr/Sonarr are deliberately not notified: they run on the seedbox and only
ever see the seedbox copy (/home/<user>/...), never this library.
"""
import logging
from typing import Dict, List

from media_manager.database import AsyncSessionLocal
from media_manager.integrations.emby import EmbyClient
from media_manager.integrations.jellyfin import JellyfinClient
from media_manager.integrations.plex import PlexClient
from media_manager.playback_matching import parse_path_mappings, to_remote_path
from media_manager.web.services.stream_activity import _configs

logger = logging.getLogger(__name__)


def updates_for(old_path: str, new_path: str) -> List[Dict[str, str]]:
    if old_path == new_path:
        return [{"Path": new_path, "UpdateType": "Modified"}]
    return [{"Path": old_path, "UpdateType": "Deleted"}, {"Path": new_path, "UpdateType": "Created"}]


async def notify_media_servers(old_path: str, new_path: str) -> Dict[str, bool]:
    """Best effort: failures are logged, never raised (the swap already succeeded)."""
    from media_manager.web.api import integrations as integrations_module
    results: Dict[str, bool] = {}
    try:
        async with AsyncSessionLocal() as db:
            configs = await _configs(db)
    except Exception as e:
        logger.warning(f"[TRANSCODE_REFRESH] Could not load media-server configs: {e}")
        return results
    for config in configs:
        mappings = parse_path_mappings(config.extra_json)
        old_remote, new_remote = to_remote_path(old_path, mappings), to_remote_path(new_path, mappings)
        try:
            if config.service_name == "plex":
                client = PlexClient(config.api_url, config.api_key_encrypted, http_client=integrations_module.SHARED_HTTP_CLIENT)
                ok = await client.refresh_path(new_remote)
            else:
                cls = JellyfinClient if config.service_name == "jellyfin" else EmbyClient
                client = cls(config.api_url, config.api_key_encrypted, http_client=integrations_module.SHARED_HTTP_CLIENT)
                ok = await client.notify_media_updated(updates_for(old_remote, new_remote))
        except Exception as e:
            logger.warning(f"[TRANSCODE_REFRESH] {config.service_name} refresh failed: {e}")
            ok = False
        results[config.service_name] = ok
    logger.info(f"[TRANSCODE_REFRESH] {new_path}: {results}")
    return results
