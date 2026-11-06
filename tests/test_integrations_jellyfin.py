import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from media_manager.integrations.jellyfin import JellyfinClient, _parse_jellyfin_datetime


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


PATCH_TARGET = "media_manager.integrations.jellyfin.httpx.AsyncClient"


class TestParseJellyfinDatetime(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_parse_jellyfin_datetime(None))

    def test_z_suffix_with_seven_digit_fraction(self):
        parsed = _parse_jellyfin_datetime("2026-01-15T10:30:00.1234567Z")
        self.assertEqual(parsed, datetime(2026, 1, 15, 10, 30, 0, 123456))

    def test_invalid_string_returns_none(self):
        self.assertIsNone(_parse_jellyfin_datetime("garbage"))


class TestJellyfinClientBasics(unittest.TestCase):
    def test_url_trailing_slash_stripped(self):
        client = JellyfinClient(api_url="http://jf:8096/", api_key="k")
        self.assertEqual(client.api_url, "http://jf:8096")

    def test_headers_include_both_token_variants(self):
        client = JellyfinClient(api_url="http://jf:8096", api_key="k")
        self.assertEqual(
            client._headers(),
            {"X-Emby-Token": "k", "X-MediaBrowser-Token": "k"},
        )


class TestJellyfinGetStatus(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = JellyfinClient(api_url="http://jf", api_key="k")

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


class TestJellyfinGetUsers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = JellyfinClient(api_url="http://jf", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{"Id": "u1"}]))
        self.assertEqual(await self.client.get_users(), [{"Id": "u1"}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_users(), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_users(), [])


class TestJellyfinGetLibraryItems(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = JellyfinClient(api_url="http://jf", api_key="k")

    @patch(PATCH_TARGET)
    async def test_sums_play_counts_across_users(self, mock_ac):
        users_resp = _response(200, [{"Id": "u1"}, {"Id": "u2"}])
        u1_items = _response(200, {"Items": [
            {"Id": "m1", "Type": "Movie", "Path": "/movies/a.mkv",
             "UserData": {"PlayCount": 4, "LastPlayedDate": "2026-01-01T00:00:00.0000000Z"}},
        ]})
        u2_items = _response(200, {"Items": [
            {"Id": "m1", "Type": "Movie", "Path": "/movies/a.mkv",
             "UserData": {"PlayCount": 2, "LastPlayedDate": "2025-01-01T00:00:00.0000000Z"}},
        ]})
        mock_ac.return_value = _mock_client([users_resp, u1_items, u2_items])

        items = await self.client.get_library_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["play_count"], 6)
        self.assertEqual(items[0]["last_played_at"], datetime(2026, 1, 1))

    @patch(PATCH_TARGET)
    async def test_episode_fields_populated(self, mock_ac):
        users_resp = _response(200, [{"Id": "u1"}])
        items_resp = _response(200, {"Items": [
            {"Id": "e1", "Type": "Episode", "Path": "/tv/show/s01e01.mkv", "SeriesName": "Show",
             "ParentIndexNumber": 2, "IndexNumber": 5, "UserData": {"PlayCount": 1}},
        ]})
        mock_ac.return_value = _mock_client([users_resp, items_resp])

        items = await self.client.get_library_items()
        self.assertEqual(items[0]["media_type"], "episode")
        self.assertEqual(items[0]["series_name"], "Show")
        self.assertEqual(items[0]["season"], 2)
        self.assertEqual(items[0]["episode"], 5)

    @patch(PATCH_TARGET)
    async def test_items_missing_path_or_id_skipped(self, mock_ac):
        users_resp = _response(200, [{"Id": "u1"}])
        items_resp = _response(200, {"Items": [
            {"Id": "e1", "Type": "Movie", "UserData": {}},
            {"Type": "Movie", "Path": "/x.mkv", "UserData": {}},
        ]})
        mock_ac.return_value = _mock_client([users_resp, items_resp])

        self.assertEqual(await self.client.get_library_items(), [])

    @patch(PATCH_TARGET)
    async def test_user_without_id_skipped(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{}]))
        self.assertEqual(await self.client.get_library_items(), [])

    @patch(PATCH_TARGET)
    async def test_get_user_items_non_200_returns_empty(self, mock_ac):
        users_resp = _response(200, [{"Id": "u1"}])
        items_resp = _response(500)
        mock_ac.return_value = _mock_client([users_resp, items_resp])
        self.assertEqual(await self.client.get_library_items(), [])


if __name__ == "__main__":
    unittest.main()


class TestJellyfinClientSessionsAndNotify(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = JellyfinClient(api_url="http://srv", api_key="k")

    @patch(PATCH_TARGET)
    async def test_get_sessions_normalizes(self, mock_ac):
        payload = [
            {"NowPlayingItem": {"Path": "/media/TvShows/S/1x01.mkv"}, "TranscodingInfo": {"IsVideoDirect": False},
             "PlayState": {"IsPaused": False}},
            {"NowPlayingItem": {"Path": "/media/Movies/M.mkv"}, "TranscodingInfo": {"IsVideoDirect": True},
             "PlayState": {"IsPaused": True}},
            {"NowPlayingItem": {"Path": "/x.mkv"}},
            {"UserName": "idle-session"},
        ]
        mock_ac.return_value = _mock_client(_response(200, payload))
        self.assertEqual(await self.client.get_sessions(), [
            {"path": "/media/TvShows/S/1x01.mkv", "video_transcode": True, "state": "playing"},
            {"path": "/media/Movies/M.mkv", "video_transcode": False, "state": "paused"},
            {"path": "/x.mkv", "video_transcode": False, "state": "playing"},
        ])

    @patch(PATCH_TARGET)
    async def test_get_sessions_errors(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_sessions(), [])
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("down"))
        self.assertEqual(await self.client.get_sessions(), [])

    @patch(PATCH_TARGET)
    async def test_notify_media_updated(self, mock_ac):
        mock = _mock_client()
        mock.post.return_value = _response(204)
        mock_ac.return_value = mock
        updates = [{"Path": "/a.mkv", "UpdateType": "Created"}]
        self.assertTrue(await self.client.notify_media_updated(updates))
        self.assertEqual(mock.post.call_args.args[0], "http://srv/Library/Media/Updated")
        self.assertEqual(mock.post.call_args.kwargs["json"], {"Updates": updates})
        mock.post.return_value = _response(400)
        self.assertFalse(await self.client.notify_media_updated(updates))
        mock.post.side_effect = RuntimeError("down")
        self.assertFalse(await self.client.notify_media_updated(updates))
