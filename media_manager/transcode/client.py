"""HTTP client the host worker uses to talk to the web app (stdlib only)."""
import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

from media_manager.transcode.protocol import PROTOCOL_VERSION

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class WorkerClient:
    """
    Retries network errors and 5xx with backoff (the web pod restarts on
    every deploy); 4xx are raised as ApiError straight away.
    """

    def __init__(self, base_url: str, token: str, worker_id: str, version: str, tls_verify: bool = True,
                 timeout: float = 30.0, retries: int = 5, backoff: float = 5.0,
                 sleep: Callable[[float], None] = time.sleep, opener=None):
        self.base_url = base_url.rstrip("/") + "/api/v1/transcode/worker"
        self.token = token
        self.worker_id = worker_id
        self.version = version
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep
        self.server_version: Optional[str] = None
        context = None if tls_verify else ssl._create_unverified_context()
        self._open = opener or (lambda req, timeout: urllib.request.urlopen(req, timeout=timeout, context=context))

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
                 params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Transcode-Token": self.token, "X-Transcode-Protocol": str(PROTOCOL_VERSION),
                   "Content-Type": "application/json"}
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self._open(req, self.timeout) as res:
                    self.server_version = res.headers.get("X-Server-Version") or self.server_version
                    raw = res.read()
                    if res.status == 204 or not raw:
                        return None
                    payload = json.loads(raw)
                    self.server_version = payload.get("server_version") or self.server_version
                    return payload
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                if e.code < 500:
                    try:
                        detail = json.loads(detail).get("detail", detail)
                    except (ValueError, AttributeError):
                        pass
                    raise ApiError(e.code, str(detail))
                last_error = ApiError(e.code, detail)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_error = e
            if attempt < self.retries:
                delay = self.backoff * (2 ** attempt)
                logger.warning(f"[TRANSCODE_WORKER] {method} {path} failed ({last_error}); retrying in {delay:.0f}s")
                self.sleep(delay)
        raise last_error  # type: ignore[misc]

    def _with_id(self, **fields) -> Dict[str, Any]:
        return {"worker_id": self.worker_id, **fields}

    def heartbeat(self, detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request("POST", "/heartbeat", {"worker_id": self.worker_id, "version": self.version,
                                                    "detail": detail or {}}) or {}

    def claim(self) -> Optional[Dict[str, Any]]:
        return self._request("POST", "/claim", self._with_id())

    def inflight(self):
        return (self._request("GET", "/inflight", params={"worker_id": self.worker_id}) or {}).get("jobs", [])

    def progress(self, job_id: str, progress_pct=None, fps=None, eta_seconds=None) -> Dict[str, Any]:
        return self._request("POST", f"/jobs/{job_id}/progress",
                             self._with_id(progress_pct=progress_pct, fps=fps, eta_seconds=eta_seconds)) or {}

    def phase(self, job_id: str, phase: str, **paths) -> Dict[str, Any]:
        return self._request("POST", f"/jobs/{job_id}/phase", self._with_id(phase=phase, **paths)) or {}

    def complete(self, job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", f"/jobs/{job_id}/complete", self._with_id(**payload)) or {}

    def fail(self, job_id: str, error_code: str, message: str = "") -> Dict[str, Any]:
        return self._request("POST", f"/jobs/{job_id}/fail",
                             self._with_id(error_code=error_code, message=message[:4000])) or {}
