import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock

from media_manager.transcode.client import ApiError, WorkerClient
from media_manager.transcode.protocol import PROTOCOL_VERSION


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status = status
        self._raw = b"" if body is None else json.dumps(body).encode()
        self.headers = headers or {}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body.encode()))


class TestWorkerClient(unittest.TestCase):
    def _client(self, responses, retries=2):
        calls = []

        def opener(req, timeout):
            calls.append(req)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        sleep = MagicMock()
        client = WorkerClient("https://app/", "tok", "w1", "1.0", retries=retries, backoff=1, sleep=sleep, opener=opener)
        return client, calls, sleep

    def test_headers_body_and_server_version(self):
        client, calls, _ = self._client([FakeResponse(body={"server_version": "2.0", "pause": False})])
        self.assertEqual(client.heartbeat({"pid": 1})["pause"], False)
        req = calls[0]
        self.assertEqual(req.full_url, "https://app/api/v1/transcode/worker/heartbeat")
        self.assertEqual(req.get_header("X-transcode-token"), "tok")
        self.assertEqual(req.get_header("X-transcode-protocol"), str(PROTOCOL_VERSION))
        self.assertEqual(json.loads(req.data), {"worker_id": "w1", "version": "1.0", "detail": {"pid": 1}})
        self.assertEqual(client.server_version, "2.0")

    def test_204_claim_and_header_version(self):
        client, _, _ = self._client([FakeResponse(status=204, headers={"X-Server-Version": "3.0"})])
        self.assertIsNone(client.claim())
        self.assertEqual(client.server_version, "3.0")

    def test_endpoints(self):
        client, calls, _ = self._client([FakeResponse(body={"jobs": [{"id": "j"}]}), FakeResponse(body={}),
                                         FakeResponse(body={}), FakeResponse(body={}), FakeResponse(body={})])
        self.assertEqual(client.inflight(), [{"id": "j"}])
        self.assertIn("worker_id=w1", calls[0].full_url)
        client.progress("j", 5, 100.0, 60)
        client.phase("j", "installing", temp_path="/t")
        client.complete("j", {"output_path": "/o"})
        client.fail("j", "not_smaller", "x" * 5000)
        self.assertEqual(json.loads(calls[2].data), {"worker_id": "w1", "phase": "installing", "temp_path": "/t"})
        self.assertEqual(len(json.loads(calls[4].data)["message"]), 4000)
        self.assertTrue(calls[1].full_url.endswith("/jobs/j/progress"))

    def test_4xx_raises_immediately_with_detail(self):
        client, calls, sleep = self._client([http_error(409, json.dumps({"detail": "leased elsewhere"}))])
        with self.assertRaises(ApiError) as ctx:
            client.claim()
        self.assertEqual((ctx.exception.status, ctx.exception.detail), (409, "leased elsewhere"))
        sleep.assert_not_called()
        client, _, _ = self._client([http_error(401, "not json")])
        with self.assertRaises(ApiError) as ctx:
            client.claim()
        self.assertEqual(ctx.exception.detail, "not json")

    def test_5xx_and_network_errors_retry_with_backoff(self):
        client, calls, sleep = self._client([http_error(502, "bad gateway"), urllib.error.URLError("down"),
                                             FakeResponse(body={"ok": True})])
        self.assertEqual(client._request("POST", "/x", {}), {"ok": True})
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])

    def test_gives_up_after_retries(self):
        client, _, _ = self._client([ConnectionError("a"), ConnectionError("b")], retries=1)
        with self.assertRaises(ConnectionError):
            client.claim()

    def test_default_opener_tls_flag(self):
        client = WorkerClient("https://app", "t", "w", "1", tls_verify=False)
        self.assertTrue(callable(client._open))


if __name__ == "__main__":
    unittest.main()
