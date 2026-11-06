import logging
from contextlib import asynccontextmanager
from typing import Optional
import httpx


class BaseHTTPClient:
    """
    Shared connection-handling for the Radarr/Sonarr/SABnzbd/Syncthing/Fanart/TMDb
    clients. Each of their ~55 methods used to open `async with httpx.AsyncClient(...)`
    per call, discarding TCP/TLS connection reuse on every single round-trip to these
    services. Pass a pooled `http_client` (see web/app.py's lifespan, which builds one
    and threads it through every client constructed in web/api/integrations.py) to
    reuse it instead; omit it (as every existing test double and any standalone/CLI
    usage does) to get the old per-call ephemeral-client behavior unchanged.
    """

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        self._http_client = http_client

    @asynccontextmanager
    async def _client(self, timeout: float = 10.0):
        if self._http_client is not None:
            yield self._http_client
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                yield client

    async def _get_ok(self, url, *, timeout=5.0, headers=None, params=None, label=None):
        try:
            async with self._client(timeout=timeout) as client:
                res = await client.get(url, headers=headers, params=params)
                return res.status_code == 200
        except Exception as e:
            if label:
                logging.getLogger(type(self).__module__).warning("%s: %s", label, e)
            return False

    async def _get_json(self, url, *, timeout=10.0, headers=None, params=None, default=None, label=None, level=logging.ERROR):
        try:
            async with self._client(timeout=timeout) as client:
                res = await client.get(url, headers=headers, params=params)
                if res.status_code == 200:
                    return res.json()
                return default
        except Exception as e:
            if label:
                logging.getLogger(type(self).__module__).log(level, "%s: %s", label, e)
            return default
