import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from media_manager.integrations.plex import PlexClient


def _mock_client(responses=None, raise_exc=None):
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    if raise_exc is not None:
        mock_client.get.side_effect = raise_exc
    elif isinstance(responses, list):
        mock_client.get.side_effect = responses
    else:
        mock_client.get.return_value = responses
    return mock_client


def _response(status_code=200, json_data=None):
    res = MagicMock()
    res.status_code = status_code
    res.json.return_value = json_data if json_data is not None else {}
    return res


PATCH_TARGET = "media_manager.integrations.plex.httpx.AsyncClient"


class TestPlexClientBasics(unittest.TestCase):
    def test_url_trailing_slash_stripped(self):
        client = PlexClient(api_url="http://plex:32400/", api_key="tok")
        self.assertEqual(client.api_url, "http://plex:32400")

    def test_headers(self):
        client = PlexClient(api_url="http://plex:32400", api_key="tok")
        self.assertEqual(client._headers(), {"X-Plex-Token": "tok", "Accept": "application/json"})


class TestPlexGetStatus(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = PlexClient(api_url="http://plex", api_key="tok")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200))
        self.assertTrue(await self.client.get_status())

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(401))
        self.assertFalse(await self.client.get_status())

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertFalse(await self.client.get_status())


class TestPlexGetSections(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = PlexClient(api_url="http://plex", api_key="tok")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        payload = {"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}]}}
        mock_ac.return_value = _mock_client(_response(200, payload))
        self.assertEqual(await self.client.get_sections(), [{"key": "1", "type": "movie"}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_sections(), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_sections(), [])


class TestPlexParseItem(unittest.TestCase):
    def setUp(self):
        self.client = PlexClient(api_url="http://plex", api_key="tok")

    def test_movie_entry(self):
        entry = {
            "ratingKey": "100",
            "viewCount": 3,
            "lastViewedAt": 1700000000,
            "Media": [{"Part": [{"file": "/data/Movies/Fight Club.mkv"}]}],
        }
        parsed = self.client._parse_item(entry, "movie")
        self.assertEqual(parsed["path"], "/data/Movies/Fight Club.mkv")
        self.assertEqual(parsed["external_id"], "100")
        self.assertEqual(parsed["play_count"], 3)
        self.assertEqual(parsed["media_type"], "movie")
        self.assertEqual(parsed["last_played_at"], datetime.utcfromtimestamp(1700000000))

    def test_episode_entry(self):
        entry = {
            "ratingKey": "200",
            "grandparentTitle": "The Wire",
            "parentIndex": 1,
            "index": 2,
            "Media": [{"Part": [{"file": "/data/TV/The Wire/S01E02.mkv"}]}],
        }
        parsed = self.client._parse_item(entry, "episode")
        self.assertEqual(parsed["series_name"], "The Wire")
        self.assertEqual(parsed["season"], 1)
        self.assertEqual(parsed["episode"], 2)
        self.assertEqual(parsed["play_count"], 0)
        self.assertIsNone(parsed["last_played_at"])

    def test_no_file_returns_none(self):
        self.assertIsNone(self.client._parse_item({"Media": []}, "movie"))

    def test_missing_media_key_returns_none(self):
        self.assertIsNone(self.client._parse_item({}, "movie"))


class TestPlexGetLibraryItems(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = PlexClient(api_url="http://plex", api_key="tok")

    @patch(PATCH_TARGET)
    async def test_merges_movie_and_show_sections(self, mock_ac):
        sections_resp = _response(200, {
            "MediaContainer": {"Directory": [
                {"key": "1", "type": "movie"},
                {"key": "2", "type": "show"},
                {"key": "3", "type": "artist"},  # ignored - not movie/show
                {"type": "movie"},  # ignored - no key
            ]}
        })
        movie_items_resp = _response(200, {"MediaContainer": {"Metadata": [
            {"ratingKey": "10", "viewCount": 1, "Media": [{"Part": [{"file": "/m/a.mkv"}]}]},
        ]}})
        episode_items_resp = _response(200, {"MediaContainer": {"Metadata": [
            {"ratingKey": "20", "grandparentTitle": "Show", "parentIndex": 1, "index": 1,
             "Media": [{"Part": [{"file": "/tv/show/s01e01.mkv"}]}]},
        ]}})
        mock_ac.return_value = _mock_client([sections_resp, movie_items_resp, episode_items_resp])

        items = await self.client.get_library_items()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["media_type"], "movie")
        self.assertEqual(items[1]["media_type"], "episode")
        self.assertEqual(items[1]["series_name"], "Show")

    @patch(PATCH_TARGET)
    async def test_no_sections_returns_empty(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"MediaContainer": {"Directory": []}}))
        self.assertEqual(await self.client.get_library_items(), [])


if __name__ == "__main__":
    unittest.main()


class TestPlexSessionsAndRefresh(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = PlexClient(api_url="http://plex", api_key="k")

    @patch(PATCH_TARGET)
    async def test_get_sessions_normalizes(self, mock_ac):
        payload = {"MediaContainer": {"Metadata": [
            {"Media": [{"Part": [{"file": "/media/Movies/A/A.mkv"}]}],
             "TranscodeSession": {"videoDecision": "transcode"}, "Player": {"state": "playing"}},
            {"Media": [], "Player": {"state": "paused"}},
        ]}}
        mock_ac.return_value = _mock_client(_response(200, payload))
        self.assertEqual(await self.client.get_sessions(), [
            {"path": "/media/Movies/A/A.mkv", "video_transcode": True, "state": "playing"},
            {"path": None, "video_transcode": False, "state": "paused"},
        ])

    @patch(PATCH_TARGET)
    async def test_get_sessions_errors(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(401))
        self.assertEqual(await self.client.get_sessions(), [])
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("down"))
        self.assertEqual(await self.client.get_sessions(), [])

    @patch(PATCH_TARGET)
    async def test_refresh_path_targets_matching_section_folder(self, mock_ac):
        sections = {"MediaContainer": {"Directory": [
            {"key": "1", "Location": [{"path": "/media/TvShows"}]},
            {"key": "2", "Location": [{"path": "/media/Movies/"}]},
        ]}}
        mock = _mock_client([_response(200, sections), _response(200)])
        mock_ac.return_value = mock
        self.assertTrue(await self.client.refresh_path("/media/Movies/A (2000)/A (2000).mkv"))
        call = mock.get.call_args_list[1]
        self.assertEqual(call.args[0], "http://plex/library/sections/2/refresh")
        self.assertEqual(call.kwargs["params"], {"path": "/media/Movies/A (2000)"})

    @patch(PATCH_TARGET)
    async def test_refresh_path_no_section_or_error(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"MediaContainer": {"Directory": [{"key": "1"}]}}))
        self.assertFalse(await self.client.refresh_path("/elsewhere/x.mkv"))
        sections = {"MediaContainer": {"Directory": [{"key": "2", "Location": [{"path": "/mnt/M"}]}]}}
        mock_ac.return_value = _mock_client([_response(200, sections), RuntimeError("boom")])
        self.assertFalse(await self.client.refresh_path("/mnt/M/x.mkv"))
