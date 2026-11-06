import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from media_manager.integrations.fanart import FanartClient


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
    res.text = "error"
    return res


class TestFanartClientInit(unittest.TestCase):
    def test_default_url_and_key_strip(self):
        client = FanartClient(api_key="  abc123  ")
        self.assertEqual(client.api_url, "https://webservice.fanart.tv/v3")
        self.assertEqual(client.api_key, "abc123")

    def test_custom_url_trailing_slash_stripped(self):
        client = FanartClient(api_url="https://custom.example.com/", api_key="k")
        self.assertEqual(client.api_url, "https://custom.example.com")

    def test_empty_url_falls_back_to_default(self):
        client = FanartClient(api_url="", api_key="k")
        self.assertEqual(client.api_url, "https://webservice.fanart.tv/v3")


class TestFanartGetStatus(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key_returns_false(self):
        client = FanartClient(api_key="")
        self.assertFalse(await client.get_status())

    @patch("media_manager.integrations.fanart.httpx.AsyncClient")
    async def test_status_200_returns_true(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200))
        client = FanartClient(api_key="k")
        self.assertTrue(await client.get_status())

    @patch("media_manager.integrations.fanart.httpx.AsyncClient")
    async def test_status_404_returns_true(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(404))
        client = FanartClient(api_key="k")
        self.assertTrue(await client.get_status())

    @patch("media_manager.integrations.fanart.httpx.AsyncClient")
    async def test_status_500_returns_false(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        client = FanartClient(api_key="k")
        self.assertFalse(await client.get_status())

    @patch("media_manager.integrations.fanart.httpx.AsyncClient")
    async def test_status_exception_returns_false(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = FanartClient(api_key="k")
        self.assertFalse(await client.get_status())


class TestFanartGetMovieImages(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key_returns_empty(self):
        client = FanartClient(api_key="")
        self.assertEqual(await client.get_movie_images("550"), {})

    async def test_no_id_returns_empty(self):
        client = FanartClient(api_key="k")
        self.assertEqual(await client.get_movie_images(""), {})

    @patch("media_manager.integrations.fanart.httpx.AsyncClient")
    async def test_success_returns_json(self, mock_ac):
        payload = {"movieposter": [{"url": "http://x/poster.jpg"}]}
        mock_ac.return_value = _mock_client(_response(200, payload))
        client = FanartClient(api_key="k")
        self.assertEqual(await client.get_movie_images("550"), payload)

    @patch("media_manager.integrations.fanart.httpx.AsyncClient")
    async def test_non_200_returns_empty(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(404))
        client = FanartClient(api_key="k")
        self.assertEqual(await client.get_movie_images("550"), {})

    @patch("media_manager.integrations.fanart.httpx.AsyncClient")
    async def test_exception_returns_empty(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = FanartClient(api_key="k")
        self.assertEqual(await client.get_movie_images("550"), {})


class TestFanartGetTvImages(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key_returns_empty(self):
        client = FanartClient(api_key="")
        self.assertEqual(await client.get_tv_images("123"), {})

    async def test_no_id_returns_empty(self):
        client = FanartClient(api_key="k")
        self.assertEqual(await client.get_tv_images(""), {})

    @patch("media_manager.integrations.fanart.httpx.AsyncClient")
    async def test_success_returns_json(self, mock_ac):
        payload = {"tvposter": [{"url": "http://x/poster.jpg"}]}
        mock_ac.return_value = _mock_client(_response(200, payload))
        client = FanartClient(api_key="k")
        self.assertEqual(await client.get_tv_images("123"), payload)

    @patch("media_manager.integrations.fanart.httpx.AsyncClient")
    async def test_non_200_returns_empty(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        client = FanartClient(api_key="k")
        self.assertEqual(await client.get_tv_images("123"), {})

    @patch("media_manager.integrations.fanart.httpx.AsyncClient")
    async def test_exception_returns_empty(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = FanartClient(api_key="k")
        self.assertEqual(await client.get_tv_images("123"), {})


class TestExtractBestArtwork(unittest.TestCase):
    def setUp(self):
        self.client = FanartClient(api_key="k")

    def test_empty_data_returns_all_none(self):
        result = self.client.extract_best_artwork({}, media_type="movie")
        self.assertEqual(result, {"poster": None, "background": None, "logo": None, "banner": None})

    def test_movie_picks_movieposter(self):
        data = {
            "movieposter": [{"url": "http://x/poster.jpg"}],
            "moviebackground": [{"url": "http://x/bg.jpg"}],
            "hdmovielogo": [{"url": "http://x/logo.png"}],
            "moviebanner": [{"url": "http://x/banner.jpg"}],
        }
        result = self.client.extract_best_artwork(data, media_type="movie")
        self.assertEqual(result["poster"], "http://x/poster.jpg")
        self.assertEqual(result["background"], "http://x/bg.jpg")
        self.assertEqual(result["logo"], "http://x/logo.png")
        self.assertEqual(result["banner"], "http://x/banner.jpg")

    def test_movie_falls_back_to_moviethumb_and_movielogo(self):
        data = {
            "moviethumb": [{"url": "http://x/thumb.jpg"}],
            "movielogo": [{"url": "http://x/logo2.png"}],
        }
        result = self.client.extract_best_artwork(data, media_type="movie")
        self.assertEqual(result["poster"], "http://x/thumb.jpg")
        self.assertEqual(result["logo"], "http://x/logo2.png")
        self.assertIsNone(result["background"])
        self.assertIsNone(result["banner"])

    def test_tv_picks_tvposter_and_hdtvlogo(self):
        data = {
            "tvposter": [{"url": "http://x/tvposter.jpg"}],
            "showbackground": [{"url": "http://x/tvbg.jpg"}],
            "hdtvlogo": [{"url": "http://x/tvlogo.png"}],
            "tvbanner": [{"url": "http://x/tvbanner.jpg"}],
        }
        result = self.client.extract_best_artwork(data, media_type="tv")
        self.assertEqual(result["poster"], "http://x/tvposter.jpg")
        self.assertEqual(result["background"], "http://x/tvbg.jpg")
        self.assertEqual(result["logo"], "http://x/tvlogo.png")
        self.assertEqual(result["banner"], "http://x/tvbanner.jpg")

    def test_tv_falls_back_to_showbackground_and_tvlogo(self):
        data = {
            "showbackground": [{"url": "http://x/fallback.jpg"}],
            "tvlogo": [{"url": "http://x/tvlogo2.png"}],
        }
        result = self.client.extract_best_artwork(data, media_type="tv")
        self.assertEqual(result["poster"], "http://x/fallback.jpg")
        self.assertEqual(result["logo"], "http://x/tvlogo2.png")

    def test_missing_keys_return_none(self):
        result = self.client.extract_best_artwork({"unrelated": []}, media_type="movie")
        self.assertIsNone(result["poster"])
        self.assertIsNone(result["background"])
        self.assertIsNone(result["logo"])
        self.assertIsNone(result["banner"])


if __name__ == "__main__":
    unittest.main()
