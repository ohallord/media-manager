import logging
from typing import Dict, Any, Optional
import httpx

from media_manager.integrations.base import BaseHTTPClient

logger = logging.getLogger(__name__)

class FanartClient(BaseHTTPClient):
    def __init__(self, api_url: str = "https://webservice.fanart.tv/v3", api_key: str = "", http_client: Optional[httpx.AsyncClient] = None):
        super().__init__(http_client)
        self.api_url = (api_url or "https://webservice.fanart.tv/v3").rstrip("/")
        self.api_key = api_key.strip()

    async def get_status(self) -> bool:
        """
        Tests connection to Fanart.tv API using the configured API Key.
        """
        if not self.api_key:
            return False
        try:
            async with self._client(timeout=5.0) as client:
                # Test with Fight Club TMDb ID 550
                res = await client.get(
                    f"{self.api_url}/movies/550",
                    params={"api_key": self.api_key}
                )
                return res.status_code in [200, 404]
        except Exception as e:
            logger.warning(f"Fanart.tv connection status check failed: {e}")
            return False

    async def get_movie_images(self, tmdb_or_imdb_id: str) -> Dict[str, Any]:
        """
        Fetches movie artwork (posters, backgrounds, logos, clearart) from Fanart.tv
        """
        if not self.api_key or not tmdb_or_imdb_id:
            return {}
        data = await self._get_json(
            f"{self.api_url}/movies/{tmdb_or_imdb_id}", timeout=8.0,
            params={"api_key": self.api_key}, default={},
            label=f"Fanart.tv get_movie_images error for {tmdb_or_imdb_id}",
        )
        return data if isinstance(data, dict) else {}

    async def get_tv_images(self, tvdb_id: str) -> Dict[str, Any]:
        """
        Fetches TV show artwork (posters, backdrops, clearlogos, banners) from Fanart.tv
        """
        if not self.api_key or not tvdb_id:
            return {}
        data = await self._get_json(
            f"{self.api_url}/tv/{tvdb_id}", timeout=8.0,
            params={"api_key": self.api_key}, default={},
            label=f"Fanart.tv get_tv_images error for {tvdb_id}",
        )
        return data if isinstance(data, dict) else {}

    def extract_best_artwork(self, data: Dict[str, Any], media_type: str = "movie") -> Dict[str, Optional[str]]:
        """
        Parses Fanart.tv API response JSON and extracts best poster, background, logo, and banner URLs.
        """
        if not data:
            return {"poster": None, "background": None, "logo": None, "banner": None}

        def pick_url(keys: list) -> Optional[str]:
            for k in keys:
                items = data.get(k, [])
                if items and isinstance(items, list) and len(items) > 0:
                    # Pick highest rated or first image
                    return items[0].get("url")
            return None

        if media_type == "movie":
            poster = pick_url(["movieposter", "moviethumb"])
            background = pick_url(["moviebackground"])
            logo = pick_url(["hdmovielogo", "movielogo"])
            banner = pick_url(["moviebanner"])
        else:
            poster = pick_url(["tvposter", "showbackground"])
            background = pick_url(["showbackground"])
            logo = pick_url(["hdtvlogo", "tvlogo"])
            banner = pick_url(["tvbanner"])

        return {
            "poster": poster,
            "background": background,
            "logo": logo,
            "banner": banner
        }
