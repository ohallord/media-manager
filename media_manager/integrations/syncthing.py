import logging
from typing import Dict, Any, List, Optional
import httpx

from media_manager.integrations.base import BaseHTTPClient

logger = logging.getLogger(__name__)

class SyncthingClient(BaseHTTPClient):
    def __init__(self, api_url: str, api_key: str, http_client: Optional[httpx.AsyncClient] = None):
        super().__init__(http_client)
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> Dict[str, str]:
        return {"X-API-Key": self.api_key}

    async def get_status(self) -> bool:
        data = await self._get_json(
            f"{self.api_url}/rest/system/version", timeout=5.0, headers=self._headers(),
            label="Syncthing connection status failed", level=logging.WARNING,
        )
        return isinstance(data, dict) and "version" in data

    async def get_folders(self) -> List[Dict[str, Any]]:
        return await self._get_json(
            f"{self.api_url}/rest/config/folders", timeout=5.0, headers=self._headers(),
            default=[], label="Syncthing get_folders error", level=logging.WARNING,
        )

    async def get_db_status(self, folder_id: str) -> Dict[str, Any]:
        return await self._get_json(
            f"{self.api_url}/rest/db/status", timeout=5.0, headers=self._headers(),
            params={"folder": folder_id}, default={},
            label=f"Syncthing get_db_status error for folder {folder_id}", level=logging.WARNING,
        )

    async def get_db_need(self, folder_id: str) -> Dict[str, Any]:
        return await self._get_json(
            f"{self.api_url}/rest/db/need", timeout=5.0, headers=self._headers(),
            params={"folder": folder_id}, default={},
            label=f"Syncthing get_db_need error for folder {folder_id}", level=logging.WARNING,
        )

    async def get_connections(self) -> Dict[str, Any]:
        return await self._get_json(
            f"{self.api_url}/rest/system/connections", timeout=5.0, headers=self._headers(),
            default={}, label="Syncthing get_connections error", level=logging.WARNING,
        )

    async def get_events(self, events: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        params = {}
        if events:
            params["events"] = events
        if limit:
            params["limit"] = limit
        return await self._get_json(
            f"{self.api_url}/rest/events", timeout=10.0, headers=self._headers(), params=params,
            default=[], label="Syncthing get_events error", level=logging.WARNING,
        )
