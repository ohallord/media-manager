import logging
from typing import List, Dict, Any, Optional
import httpx

from media_manager.integrations.base import BaseHTTPClient

logger = logging.getLogger(__name__)

class RadarrClient(BaseHTTPClient):
    def __init__(self, api_url: str, api_key: str, http_client: Optional[httpx.AsyncClient] = None):
        super().__init__(http_client)
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> Dict[str, str]:
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    async def get_status(self) -> bool:
        return await self._get_ok(
            f"{self.api_url}/api/v3/system/status", timeout=5.0, headers=self._headers(),
            label="Radarr connection status failed",
        )

    async def get_health(self) -> Optional[List[Dict[str, Any]]]:
        """Radarr's own health checks (System > Status); None when it can't be read."""
        data = await self._get_json(
            f"{self.api_url}/api/v3/health", timeout=5.0, headers=self._headers(),
            label="Radarr get_health error", level=logging.WARNING,
        )
        return data if isinstance(data, list) else None

    async def get_profiles(self) -> List[Dict[str, Any]]:
        return await self._get_json(
            f"{self.api_url}/api/v3/qualityprofile", timeout=5.0, headers=self._headers(),
            default=[], label="Radarr get_profiles error",
        )

    async def get_root_folders(self) -> List[Dict[str, Any]]:
        return await self._get_json(
            f"{self.api_url}/api/v3/rootfolder", timeout=5.0, headers=self._headers(),
            default=[], label="Radarr get_root_folders error",
        )

    async def get_history(self, page_size: int = 500) -> List[Dict[str, Any]]:
        body = await self._get_json(
            f"{self.api_url}/api/v3/history", timeout=10.0, headers=self._headers(),
            params={"page": 1, "pageSize": page_size, "sortKey": "date", "sortDirection": "descending"},
            default={}, label="Radarr get_history error", level=logging.WARNING,
        )
        return body.get("records", []) if isinstance(body, dict) else []

    async def search_movies(self, term: str) -> List[Dict[str, Any]]:
        return await self._get_json(
            f"{self.api_url}/api/v3/movie/lookup", timeout=10.0, headers=self._headers(),
            params={"term": term}, default=[], label="Radarr search_movies error",
        )

    async def lookup_movie_by_tmdb_id(self, tmdb_id: int) -> Optional[Dict[str, Any]]:
        """
        Looks up a single movie by TMDb ID via Radarr's lookup service, returning it
        already shaped for add_movie() (title, titleSlug, images, tmdbId, year, etc.).
        """
        data = await self._get_json(
            f"{self.api_url}/api/v3/movie/lookup/tmdb", timeout=10.0, headers=self._headers(),
            params={"tmdbId": tmdb_id}, label="Radarr lookup_movie_by_tmdb_id error",
        )
        return data if isinstance(data, dict) else None

    async def add_movie(
        self,
        movie_data: Dict[str, Any],
        quality_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        search_now: bool = True
    ) -> Dict[str, Any]:
        payload = {
            "title": movie_data.get("title"),
            "qualityProfileId": quality_profile_id,
            "titleSlug": movie_data.get("titleSlug"),
            "images": movie_data.get("images", []),
            "tmdbId": movie_data.get("tmdbId"),
            "year": movie_data.get("year"),
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "addOptions": {"searchForMovie": search_now}
        }
        try:
            async with self._client(timeout=10.0) as client:
                res = await client.post(
                    f"{self.api_url}/api/v3/movie",
                    json=payload,
                    headers=self._headers()
                )
                if res.status_code in [200, 201]:
                    return {"success": True, "data": res.json()}
                logger.error(f"Radarr add_movie returned {res.status_code}: {res.text}")
                return {"success": False, "error": res.text}
        except Exception as e:
            logger.error(f"Radarr add_movie exception: {e}")
            return {"success": False, "error": str(e)}

    async def delete_movie(self, movie_id: int, delete_files: bool = False) -> bool:
        try:
            async with self._client(timeout=10.0) as client:
                res = await client.delete(
                    f"{self.api_url}/api/v3/movie/{movie_id}",
                    params={"deleteFiles": delete_files},
                    headers=self._headers()
                )
                return res.status_code == 200
        except Exception as e:
            logger.error(f"Radarr delete_movie error: {e}")
            return False

    async def delete_movie_file(self, movie_file_id: int) -> bool:
        """Removes one movie file record (and the file on Radarr's side) so the
        movie counts as missing again and a MoviesSearch will grab it."""
        try:
            async with self._client(timeout=10.0) as client:
                res = await client.delete(
                    f"{self.api_url}/api/v3/moviefile/{movie_file_id}",
                    headers=self._headers()
                )
                return res.status_code == 200
        except Exception as e:
            logger.error(f"Radarr delete_movie_file error: {e}")
            return False

    async def set_movies_monitored(self, movie_ids: List[int], monitored: bool = True) -> bool:
        """Bulk (un)monitor via the movie editor. Radarr's "unmonitor deleted
        movies" setting unmonitors a movie when its file record is deleted."""
        try:
            async with self._client(timeout=10.0) as client:
                res = await client.put(
                    f"{self.api_url}/api/v3/movie/editor",
                    json={"movieIds": movie_ids, "monitored": monitored},
                    headers=self._headers()
                )
                return res.status_code in (200, 202)
        except Exception as e:
            logger.error(f"Radarr set_movies_monitored error: {e}")
            return False

    async def get_movies(self) -> List[Dict[str, Any]]:
        return await self._get_json(
            f"{self.api_url}/api/v3/movie", timeout=5.0, headers=self._headers(),
            default=[], label="Radarr get_movies error",
        )

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

    async def get_calendar(self, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        return await self._get_json(
            f"{self.api_url}/api/v3/calendar", timeout=10.0, headers=self._headers(),
            params={"start": start_date, "end": end_date},
            default=[], label="Radarr get_calendar error",
        )

    async def get_queue(self, page_size: int = 200) -> List[Dict[str, Any]]:
        """
        Gets the current download queue (grabbed releases still downloading or
        pending import), with movie details included per record.
        """
        body = await self._get_json(
            f"{self.api_url}/api/v3/queue", timeout=10.0, headers=self._headers(),
            params={"pageSize": page_size, "includeMovie": "true"},
            default={}, label="Radarr get_queue error",
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
            logger.error(f"Radarr delete_queue_item error: {e}")
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
            logger.error(f"Radarr grab_queue_item error: {e}")
            return False
