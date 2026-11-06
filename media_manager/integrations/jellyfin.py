import logging
from typing import Any, Dict, List, Optional
import httpx

from media_manager.integrations.base import BaseHTTPClient
from media_manager.integrations.emby import _parse_emby_datetime as _parse_jellyfin_datetime

logger = logging.getLogger(__name__)


class JellyfinClient(BaseHTTPClient):
    """
    Jellyfin Server client used to pull per-item play counts/last-played
    timestamps for the playback-tracking sync (media_manager/web/services/
    playback_sync.py). Jellyfin's API is Emby-compatible (Jellyfin forked from
    Emby), so this mirrors EmbyClient's shape; kept as its own file rather than
    a shared base class, matching how this codebase keeps Radarr/Sonarr separate
    despite their similarity. Sends both X-Emby-Token and X-MediaBrowser-Token
    since Jellyfin has changed its preferred auth header across versions while
    keeping the legacy one working.
    """

    def __init__(self, api_url: str, api_key: str, http_client: Optional[httpx.AsyncClient] = None):
        super().__init__(http_client)
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> Dict[str, str]:
        return {"X-Emby-Token": self.api_key, "X-MediaBrowser-Token": self.api_key}

    async def get_status(self) -> bool:
        return await self._get_ok(
            f"{self.api_url}/System/Info", timeout=5.0, headers=self._headers(),
            label="Jellyfin connection status failed",
        )

    async def get_users(self) -> List[Dict[str, Any]]:
        return await self._get_json(
            f"{self.api_url}/Users", timeout=10.0, headers=self._headers(),
            default=[], label="Jellyfin get_users error",
        )

    async def _get_user_items(self, user_id: str) -> List[Dict[str, Any]]:
        body = await self._get_json(
            f"{self.api_url}/Users/{user_id}/Items", timeout=30.0, headers=self._headers(),
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Episode",
                "Fields": "Path,UserData,SeriesName,ParentIndexNumber,IndexNumber",
            },
            default={}, label=f"Jellyfin _get_user_items error (user {user_id})",
        )
        return body.get("Items", []) if isinstance(body, dict) else []

    async def get_library_items(self) -> List[Dict[str, Any]]:
        """
        Returns a normalized list of {"path", "external_id", "play_count",
        "last_played_at", "media_type": "movie"|"episode", "series_name",
        "season", "episode"}. Play counts are summed across every local
        Jellyfin user for the same item; last_played_at is the max across users.
        """
        merged: Dict[str, Dict[str, Any]] = {}

        for user in await self.get_users():
            user_id = user.get("Id")
            if not user_id:
                continue
            for entry in await self._get_user_items(user_id):
                path = entry.get("Path")
                item_id = entry.get("Id")
                if not path or item_id is None:
                    continue

                user_data = entry.get("UserData") or {}
                play_count = int(user_data.get("PlayCount") or 0)
                last_played_at = _parse_jellyfin_datetime(user_data.get("LastPlayedDate"))

                item_type = entry.get("Type")
                media_type = "movie" if item_type == "Movie" else "episode"

                existing = merged.get(item_id)
                if existing:
                    existing["play_count"] += play_count
                    if last_played_at and (not existing["last_played_at"] or last_played_at > existing["last_played_at"]):
                        existing["last_played_at"] = last_played_at
                else:
                    merged[item_id] = {
                        "path": path,
                        "external_id": str(item_id),
                        "play_count": play_count,
                        "last_played_at": last_played_at,
                        "media_type": media_type,
                        "series_name": entry.get("SeriesName") if media_type == "episode" else None,
                        "season": entry.get("ParentIndexNumber") if media_type == "episode" else None,
                        "episode": entry.get("IndexNumber") if media_type == "episode" else None,
                    }

        return list(merged.values())

    async def get_sessions(self) -> List[Dict[str, Any]]:
        """
        Active playback sessions, normalized to {"path", "video_transcode",
        "state"}. video_transcode is True when the server is re-encoding the
        video stream (TranscodingInfo.IsVideoDirect is false).
        """
        entries = await self._get_json(
            f"{self.api_url}/Sessions", timeout=10.0, headers=self._headers(),
            params={"ActiveWithinSeconds": 120}, default=[],
            label="Jellyfin get_sessions error", level=logging.WARNING,
        ) or []
        sessions = []
        for entry in entries:
            playing = entry.get("NowPlayingItem")
            if not playing:
                continue
            transcode = entry.get("TranscodingInfo") or {}
            sessions.append({
                "path": playing.get("Path"),
                "video_transcode": bool(transcode) and not transcode.get("IsVideoDirect", True),
                "state": "paused" if (entry.get("PlayState") or {}).get("IsPaused") else "playing",
            })
        return sessions

    async def notify_media_updated(self, updates: List[Dict[str, str]]) -> bool:
        """Tells the server specific files changed: [{"Path": ..., "UpdateType": "Created|Deleted|Modified"}]."""
        try:
            async with self._client(timeout=10.0) as client:
                res = await client.post(f"{self.api_url}/Library/Media/Updated", json={"Updates": updates},
                                        headers=self._headers())
                return res.status_code in (200, 204)
        except Exception as e:
            logger.warning(f"Jellyfin notify_media_updated error: {e}")
            return False
