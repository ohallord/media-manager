import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from media_manager.integrations.syncthing import SyncthingClient


def _mock_client(response=None, raise_exc=None):
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    if raise_exc is not None:
        mock_client.get.side_effect = raise_exc
    else:
        mock_client.get.return_value = response
    return mock_client


def _response(status_code=200, json_data=None):
    res = MagicMock()
    res.status_code = status_code
    res.json.return_value = json_data if json_data is not None else {}
    return res


PATCH_TARGET = "media_manager.integrations.syncthing.httpx.AsyncClient"


class TestSyncthingClientBasics(unittest.TestCase):
    def test_url_trailing_slash_stripped(self):
        client = SyncthingClient(api_url="http://syncthing:8384/", api_key="k")
        self.assertEqual(client.api_url, "http://syncthing:8384")

    def test_headers(self):
        client = SyncthingClient(api_url="http://syncthing", api_key="mykey")
        self.assertEqual(client._headers(), {"X-API-Key": "mykey"})


class TestSyncthingGetStatus(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SyncthingClient(api_url="http://syncthing", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"version": "v1.27.0"}))
        self.assertTrue(await self.client.get_status())

    @patch(PATCH_TARGET)
    async def test_200_missing_version_key(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"other": "field"}))
        self.assertFalse(await self.client.get_status())

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertFalse(await self.client.get_status())

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertFalse(await self.client.get_status())


class TestSyncthingGetFolders(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SyncthingClient(api_url="http://syncthing", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{"id": "movies"}]))
        self.assertEqual(await self.client.get_folders(), [{"id": "movies"}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_folders(), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_folders(), [])


class TestSyncthingGetDbStatus(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SyncthingClient(api_url="http://syncthing", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"state": "idle"}))
        self.assertEqual(await self.client.get_db_status("movies"), {"state": "idle"})

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_db_status("movies"), {})

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_db_status("movies"), {})


class TestSyncthingGetDbNeed(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SyncthingClient(api_url="http://syncthing", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"need": []}))
        self.assertEqual(await self.client.get_db_need("movies"), {"need": []})

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_db_need("movies"), {})

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_db_need("movies"), {})


class TestSyncthingGetConnections(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SyncthingClient(api_url="http://syncthing", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"connections": {}}))
        self.assertEqual(await self.client.get_connections(), {"connections": {}})

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_connections(), {})

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_connections(), {})


class TestSyncthingGetEvents(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SyncthingClient(api_url="http://syncthing", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success_no_params(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{"type": "FolderSummary"}]))
        self.assertEqual(await self.client.get_events(), [{"type": "FolderSummary"}])

    @patch(PATCH_TARGET)
    async def test_success_with_params(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{"type": "FolderSummary"}]))
        result = await self.client.get_events(events="FolderSummary", limit=10)
        self.assertEqual(result, [{"type": "FolderSummary"}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_events(), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_events(), [])


if __name__ == "__main__":
    unittest.main()
