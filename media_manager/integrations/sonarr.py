import logging
from typing import List, Dict, Any, Optional
import httpx

from media_manager.integrations.base import BaseHTTPClient

logger = logging.getLogger(__name__)

class SonarrClient(BaseHTTPClient):
    def __init__(self, api_url: str, api_key: str, http_client: Optional[httpx.AsyncClient] = None):
        super().__init__(http_client)
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> Dict[str, str]:
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    async def get_status(self) -> bool:
        return await self._get_ok(
            f"{self.api_url}/api/v3/system/status", timeout=5.0, headers=self._headers(),
            label="Sonarr connection status failed",
        )

    async def get_health(self) -> Optional[List[Dict[str, Any]]]:
        """Sonarr's own health checks (System > Status); None when it can't be read."""
        data = await self._get_json(
            f"{self.api_url}/api/v3/health", timeout=5.0, headers=self._headers(),
            label="Sonarr get_health error", level=logging.WARNING,
        )
        return data if isinstance(data, list) else None

    async def get_profiles(self) -> List[Dict[str, Any]]:
        return await self._get_json(
            f"{self.api_url}/api/v3/qualityprofile", timeout=5.0, headers=self._headers(),
            default=[], label="Sonarr get_profiles error",
        )

    async def get_language_profiles(self) -> List[Dict[str, Any]]:
        return await self._get_json(
            f"{self.api_url}/api/v3/languageprofile", timeout=5.0, headers=self._headers(),
            default=[], label="Sonarr get_language_profiles error",
        )

    async def get_root_folders(self) -> List[Dict[str, Any]]:
        return await self._get_json(
            f"{self.api_url}/api/v3/rootfolder", timeout=5.0, headers=self._headers(),
            default=[], label="Sonarr get_root_folders error",
        )

    async def search_series(self, term: str) -> List[Dict[str, Any]]:
        return await self._get_json(
            f"{self.api_url}/api/v3/series/lookup", timeout=10.0, headers=self._headers(),
            params={"term": term}, default=[], label="Sonarr search_series error",
        )

    async def lookup_series_by_tvdb_id(self, tvdb_id: int) -> Optional[Dict[str, Any]]:
        """
        Looks up a single series by TVDB ID via Sonarr's lookup service (which
        supports a "tvdb:<id>" term for exact-match lookup), returning it already
        shaped for add_series() (title, titleSlug, images, tvdbId, year, etc.).
        """
        results = await self.search_series(f"tvdb:{tvdb_id}")
        return results[0] if results else None

    async def add_series(
        self,
        series_data: Dict[str, Any],
        quality_profile_id: int,
        root_folder_path: str,
        language_profile_id: Optional[int] = None,
        monitored: bool = True,
        search_now: bool = True,
        season_monitor: Optional[str] = None
    ) -> Dict[str, Any]:
        payload = {
            "title": series_data.get("title"),
            "qualityProfileId": quality_profile_id,
            "titleSlug": series_data.get("titleSlug"),
            "images": series_data.get("images", []),
            "tvdbId": series_data.get("tvdbId"),
            "year": series_data.get("year"),
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "addOptions": {
                "searchForMissingEpisodes": search_now,
                "monitor": season_monitor or "all",
            }
        }
        # Sonarr v3 rejects series without a valid languageProfileId (it defaults
        # to 0, which fails "LanguageProfileExistsValidator"); v4 dropped language
        # profiles entirely, so only set this when the instance actually has one.
        if language_profile_id is not None:
            payload["languageProfileId"] = language_profile_id
        try:
            async with self._client(timeout=10.0) as client:
                res = await client.post(
                    f"{self.api_url}/api/v3/series",
                    json=payload,
                    headers=self._headers()
                )
                if res.status_code in [200, 201]:
                    return {"success": True, "data": res.json()}
                logger.error(f"Sonarr add_series returned {res.status_code}: {res.text}")
                return {"success": False, "error": res.text}
        except Exception as e:
            logger.error(f"Sonarr add_series exception: {e}")
            return {"success": False, "error": str(e)}

    async def delete_series(self, series_id: int, delete_files: bool = False) -> bool:
        try:
            async with self._client(timeout=10.0) as client:
                res = await client.delete(
                    f"{self.api_url}/api/v3/series/{series_id}",
                    params={"deleteFiles": delete_files},
                    headers=self._headers()
                )
                return res.status_code == 200
        except Exception as e:
            logger.error(f"Sonarr delete_series error: {e}")
            return False

    async def get_series(self) -> List[Dict[str, Any]]:
        return await self._get_json(
            f"{self.api_url}/api/v3/series", timeout=5.0, headers=self._headers(),
            default=[], label="Sonarr get_series error",
        )

    async def get_episodes(self, series_id: int) -> List[Dict[str, Any]]:
        return await self._get_json(
            f"{self.api_url}/api/v3/episode", timeout=10.0, headers=self._headers(),
            params={"seriesId": series_id}, default=[], label="Sonarr get_episodes error",
        )

    async def set_episodes_monitored(self, episode_ids: List[int], monitored: bool = True) -> bool:
        """Bulk (un)monitor episodes. Sonarr's "unmonitor deleted episodes"
        setting unmonitors an episode when its file record is deleted."""
        try:
            async with self._client(timeout=10.0) as client:
                res = await client.put(
                    f"{self.api_url}/api/v3/episode/monitor",
                    json={"episodeIds": episode_ids, "monitored": monitored},
                    headers=self._headers()
                )
                return res.status_code in (200, 202)
        except Exception as e:
            logger.error(f"Sonarr set_episodes_monitored error: {e}")
            return False

    async def delete_episode_file(self, episode_file_id: int) -> bool:
        """Removes one episode file record so the episode counts as missing again."""
        try:
            async with self._client(timeout=10.0) as client:
                res = await client.delete(
                    f"{self.api_url}/api/v3/episodefile/{episode_file_id}",
                    headers=self._headers()
                )
                return res.status_code == 200
        except Exception as e:
            logger.error(f"Sonarr delete_episode_file error: {e}")
            return False

    async def trigger_command(self, name: str, **kwargs) -> Dict[str, Any]:
        payload = {"name": name, **kwargs}
        try:
            async with self._client(timeout=10.0) as client:
                res = await client.post(
                    f"{self.api_url}/api/v3/command",
                    json=payload,
                    headers=self._headers()
                )
                if res.status_code in [200, 201]:
                    return {"success": True, "data": res.json()}
                return {"success": False, "error": res.text}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_history(self, page_size: int = 500) -> List[Dict[str, Any]]:
        body = await self._get_json(
            f"{self.api_url}/api/v3/history", timeout=10.0, headers=self._headers(),
            params={"page": 1, "pageSize": page_size, "sortKey": "date", "sortDirection": "descending"},
            default={}, label="Sonarr get_history error", level=logging.WARNING,
        )
        return body.get("records", []) if isinstance(body, dict) else []

    async def get_calendar(self, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        return await self._get_json(
            f"{self.api_url}/api/v3/calendar", timeout=10.0, headers=self._headers(),
            params={"start": start_date, "end": end_date},
            default=[], label="Sonarr get_calendar error",
        )

    async def get_queue(self, page_size: int = 200) -> List[Dict[str, Any]]:
        """
        Gets the current download queue (grabbed releases still downloading or
        pending import), with series/episode details included per record.
        """
        body = await self._get_json(
            f"{self.api_url}/api/v3/queue", timeout=10.0, headers=self._headers(),
            params={"pageSize": page_size, "includeSeries": "true", "includeEpisode": "true"},
            default={}, label="Sonarr get_queue error",
        )
        return body.get("records", []) if isinstance(body, dict) else []

    async def delete_queue_item(self, queue_id: int, remove_from_client: bool = True, blocklist: bool = False) -> bool:
        try:
            async with self._client(timeout=10.0) as client:
                res = await client.delete(
                    f"{self.api_url}/api/v3/queue/{queue_id}",
                    params={"removeFromClient": str(remove_from_client).lower(), "blocklist": str(blocklist).lower()},
                    headers=self._headers()
                )
                return res.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Sonarr delete_queue_item error: {e}")
            return False

    async def grab_queue_item(self, queue_id: int) -> bool:
        try:
            async with self._client(timeout=10.0) as client:
                res = await client.post(
                    f"{self.api_url}/api/v3/queue/grab/{queue_id}",
                    headers=self._headers()
                )
                return res.status_code in (200, 201)
        except Exception as e:
            logger.error(f"Sonarr grab_queue_item error: {e}")
            return False
