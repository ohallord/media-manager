import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
import httpx

from media_manager.integrations.base import BaseHTTPClient

logger = logging.getLogger(__name__)

# Plex's numeric "type" for a flattened episode listing under
# /library/sections/{key}/all?type=4 (1=movie, 2=show, 4=episode).
PLEX_EPISODE_TYPE = 4


def _plex_container_list(body, key):
    container = body.get("MediaContainer") if isinstance(body, dict) else None
    if not isinstance(container, dict):
        return []
    value = container.get(key) or []
    return value if isinstance(value, list) else []


class PlexClient(BaseHTTPClient):
    """
    Plex Media Server client used to pull per-item play counts/last-played
    timestamps for the playback-tracking sync (media_manager/web/services/
    playback_sync.py). Only get_library_items()'s normalized shape is consumed
    by that service; everything else here is plumbing for it.
    """

    def __init__(self, api_url: str, api_key: str, http_client: Optional[httpx.AsyncClient] = None):
        super().__init__(http_client)
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> Dict[str, str]:
        return {"X-Plex-Token": self.api_key, "Accept": "application/json"}

    async def get_status(self) -> bool:
        return await self._get_ok(
            f"{self.api_url}/identity", timeout=5.0, headers=self._headers(),
            label="Plex connection status failed",
        )

    async def get_sections(self) -> List[Dict[str, Any]]:
        body = await self._get_json(
            f"{self.api_url}/library/sections", timeout=10.0, headers=self._headers(),
            default={}, label="Plex get_sections error",
        )
        return _plex_container_list(body, "Directory")

    def _parse_item(self, entry: Dict[str, Any], media_type: str) -> Optional[Dict[str, Any]]:
        media = entry.get("Media") or []
        part = (media[0].get("Part") or []) if media else []
        file_path = part[0].get("file") if part else None
        if not file_path:
            return None

        last_viewed_epoch = entry.get("lastViewedAt")
        last_played_at = (
            datetime.utcfromtimestamp(last_viewed_epoch) if isinstance(last_viewed_epoch, (int, float)) else None
        )

        item: Dict[str, Any] = {
            "path": file_path,
            "external_id": str(entry.get("ratingKey")) if entry.get("ratingKey") is not None else None,
            "play_count": int(entry.get("viewCount") or 0),
            "last_played_at": last_played_at,
            "media_type": media_type,
            "series_name": None,
            "season": None,
            "episode": None,
        }
        if media_type == "episode":
            item["series_name"] = entry.get("grandparentTitle")
            item["season"] = entry.get("parentIndex")
            item["episode"] = entry.get("index")
        return item

    async def _get_section_items(self, section_key: str, plex_type: Optional[int] = None) -> List[Dict[str, Any]]:
        params = {"type": plex_type} if plex_type is not None else None
        body = await self._get_json(
            f"{self.api_url}/library/sections/{section_key}/all", timeout=20.0,
            params=params, headers=self._headers(), default={},
            label=f"Plex get_section_items error (section {section_key})",
        )
        return _plex_container_list(body, "Metadata")

    async def get_library_items(self) -> List[Dict[str, Any]]:
        """
        Returns a normalized list of {"path", "external_id", "play_count",
        "last_played_at", "media_type": "movie"|"episode", "series_name",
        "season", "episode"} across every movie/show library section.
        """
        results: List[Dict[str, Any]] = []
        for section in await self.get_sections():
            section_key = section.get("key")
            section_type = section.get("type")
            if not section_key or section_type not in ("movie", "show"):
                continue

            if section_type == "movie":
                for entry in await self._get_section_items(section_key):
                    parsed = self._parse_item(entry, "movie")
                    if parsed:
                        results.append(parsed)
            else:
                for entry in await self._get_section_items(section_key, plex_type=PLEX_EPISODE_TYPE):
                    parsed = self._parse_item(entry, "episode")
                    if parsed:
                        results.append(parsed)
        return results

    async def get_sessions(self) -> List[Dict[str, Any]]:
        """
        Current playback sessions, normalized to {"path", "video_transcode",
        "state"}. video_transcode is True when Plex is re-encoding the video
        (i.e. using the GPU), False for direct play/stream.
        """
        body = await self._get_json(
            f"{self.api_url}/status/sessions", timeout=10.0, headers=self._headers(),
            default={}, label="Plex get_sessions error", level=logging.WARNING,
        )
        entries = _plex_container_list(body, "Metadata")
        sessions = []
        for entry in entries:
            media = entry.get("Media") or []
            part = (media[0].get("Part") or []) if media else []
            transcode = entry.get("TranscodeSession") or {}
            sessions.append({
                "path": part[0].get("file") if part else None,
                "video_transcode": transcode.get("videoDecision") == "transcode",
                "state": (entry.get("Player") or {}).get("state"),
            })
        return sessions

    async def refresh_path(self, file_path: str) -> bool:
        """Partial scan of just the folder holding file_path, in whichever section contains it."""
        folder = file_path.rsplit("/", 1)[0]
        for section in await self.get_sections():
            locations = [loc.get("path", "").rstrip("/") for loc in section.get("Location") or []]
            if not any(loc and (folder == loc or folder.startswith(loc + "/")) for loc in locations):
                continue
            return await self._get_ok(
                f"{self.api_url}/library/sections/{section.get('key')}/refresh",
                timeout=10.0, params={"path": folder}, headers=self._headers(),
                label="Plex refresh_path error",
            )
        return False
