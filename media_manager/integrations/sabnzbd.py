import logging
from typing import Dict, Any, Optional
import httpx

from media_manager.integrations.base import BaseHTTPClient

logger = logging.getLogger(__name__)

class SABnzbdClient(BaseHTTPClient):
    def __init__(self, api_url: str, api_key: str, http_client: Optional[httpx.AsyncClient] = None):
        super().__init__(http_client)
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key

    async def get_status(self) -> bool:
        try:
            async with self._client(timeout=5.0) as client:
                res = await client.get(
                    f"{self.api_url}/api",
                    params={"mode": "version", "output": "json", "apikey": self.api_key}
                )
                return res.status_code == 200 and "version" in res.text.lower()
        except Exception as e:
            logger.warning(f"SABnzbd connection status failed: {e}")
            return False

    async def get_queue(self) -> Dict[str, Any]:
        body = await self._get_json(
            f"{self.api_url}/api", timeout=5.0,
            params={"mode": "queue", "output": "json", "apikey": self.api_key},
            default={}, label="SABnzbd get_queue error", level=logging.WARNING,
        )
        return body.get("queue", {}) if isinstance(body, dict) else {}

    async def delete_job(self, nzo_id: str) -> bool:
        try:
            async with self._client(timeout=5.0) as client:
                res = await client.get(
                    f"{self.api_url}/api",
                    params={"mode": "queue", "name": "delete", "value": nzo_id, "output": "json", "apikey": self.api_key}
                )
                return res.status_code == 200
        except Exception as e:
            logger.error(f"SABnzbd delete_job error: {e}")
            return False

    async def toggle_pause(self, pause: bool = True) -> bool:
        mode = "pause" if pause else "resume"
        try:
            async with self._client(timeout=5.0) as client:
                res = await client.get(
                    f"{self.api_url}/api",
                    params={"mode": mode, "output": "json", "apikey": self.api_key}
                )
                return res.status_code == 200
        except Exception as e:
            logger.error(f"SABnzbd toggle_pause error: {e}")
            return False

    async def get_history(self) -> Dict[str, Any]:
        body = await self._get_json(
            f"{self.api_url}/api", timeout=5.0,
            params={"mode": "history", "output": "json", "apikey": self.api_key, "limit": 100},
            default={}, label="SABnzbd get_history error", level=logging.WARNING,
        )
        return body.get("history", {}) if isinstance(body, dict) else {}
