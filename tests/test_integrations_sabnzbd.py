import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from media_manager.integrations.sabnzbd import SABnzbdClient


def _mock_client(response=None, raise_exc=None):
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    if raise_exc is not None:
        mock_client.get.side_effect = raise_exc
    else:
        mock_client.get.return_value = response
    return mock_client


def _response(status_code=200, json_data=None, text=""):
    res = MagicMock()
    res.status_code = status_code
    res.json.return_value = json_data if json_data is not None else {}
    res.text = text
    return res


PATCH_TARGET = "media_manager.integrations.sabnzbd.httpx.AsyncClient"


class TestSABnzbdGetStatus(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SABnzbdClient(api_url="http://sab:8080/", api_key="k")

    def test_url_trailing_slash_stripped(self):
        self.assertEqual(self.client.api_url, "http://sab:8080")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, text='{"version": "4.0"}'))
        self.assertTrue(await self.client.get_status())

    @patch(PATCH_TARGET)
    async def test_200_but_no_version_in_body(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, text='{"error": "bad key"}'))
        self.assertFalse(await self.client.get_status())

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertFalse(await self.client.get_status())

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertFalse(await self.client.get_status())


class TestSABnzbdGetQueue(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SABnzbdClient(api_url="http://sab", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"queue": {"slots": []}}))
        self.assertEqual(await self.client.get_queue(), {"slots": []})

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_queue(), {})

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_queue(), {})


class TestSABnzbdDeleteJob(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SABnzbdClient(api_url="http://sab", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200))
        self.assertTrue(await self.client.delete_job("nzo_1"))

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertFalse(await self.client.delete_job("nzo_1"))

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertFalse(await self.client.delete_job("nzo_1"))


class TestSABnzbdTogglePause(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SABnzbdClient(api_url="http://sab", api_key="k")

    @patch(PATCH_TARGET)
    async def test_pause_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200))
        self.assertTrue(await self.client.toggle_pause(True))

    @patch(PATCH_TARGET)
    async def test_resume_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200))
        self.assertTrue(await self.client.toggle_pause(False))

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertFalse(await self.client.toggle_pause(True))

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertFalse(await self.client.toggle_pause(True))


class TestSABnzbdGetHistory(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SABnzbdClient(api_url="http://sab", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"history": {"slots": []}}))
        self.assertEqual(await self.client.get_history(), {"slots": []})

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_history(), {})

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_history(), {})


if __name__ == "__main__":
    unittest.main()
