import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from media_manager.integrations.sonarr import SonarrClient


def _mock_client(response=None, raise_exc=None):
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    if raise_exc is not None:
        mock_client.get.side_effect = raise_exc
        mock_client.post.side_effect = raise_exc
        mock_client.delete.side_effect = raise_exc
    else:
        mock_client.get.return_value = response
        mock_client.post.return_value = response
        mock_client.delete.return_value = response
    return mock_client


def _response(status_code=200, json_data=None, text="error"):
    res = MagicMock()
    res.status_code = status_code
    res.json.return_value = json_data if json_data is not None else {}
    res.text = text
    return res


PATCH_TARGET = "media_manager.integrations.sonarr.httpx.AsyncClient"


class TestSonarrClientBasics(unittest.TestCase):
    def test_url_trailing_slash_stripped(self):
        client = SonarrClient(api_url="http://sonarr:8989/", api_key="k")
        self.assertEqual(client.api_url, "http://sonarr:8989")

    def test_headers(self):
        client = SonarrClient(api_url="http://sonarr:8989", api_key="mykey")
        self.assertEqual(client._headers(), {"X-Api-Key": "mykey", "Content-Type": "application/json"})


class TestSonarrGetStatus(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200))
        self.assertTrue(await self.client.get_status())

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertFalse(await self.client.get_status())

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertFalse(await self.client.get_status())


class TestSonarrGetProfiles(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{"id": 1}]))
        self.assertEqual(await self.client.get_profiles(), [{"id": 1}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(404))
        self.assertEqual(await self.client.get_profiles(), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_profiles(), [])


class TestSonarrGetLanguageProfiles(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{"id": 1, "name": "English"}]))
        self.assertEqual(await self.client.get_language_profiles(), [{"id": 1, "name": "English"}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        # Sonarr v4 has no languageprofile endpoint at all - should look like "none configured".
        mock_ac.return_value = _mock_client(_response(404))
        self.assertEqual(await self.client.get_language_profiles(), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_language_profiles(), [])


class TestSonarrGetRootFolders(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{"path": "/tv"}]))
        self.assertEqual(await self.client.get_root_folders(), [{"path": "/tv"}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_root_folders(), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_root_folders(), [])


class TestSonarrSearchSeries(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{"title": "The Sopranos"}]))
        self.assertEqual(await self.client.search_series("Sopranos"), [{"title": "The Sopranos"}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.search_series("x"), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.search_series("x"), [])


class TestSonarrLookupSeriesByTvdbId(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success_returns_first_match_and_uses_tvdb_prefixed_term(self, mock_ac):
        mock_instance = _mock_client(_response(200, [{"title": "The Sopranos", "tvdbId": 12345}]))
        mock_ac.return_value = mock_instance
        result = await self.client.lookup_series_by_tvdb_id(12345)
        self.assertEqual(result, {"title": "The Sopranos", "tvdbId": 12345})
        _, kwargs = mock_instance.get.call_args
        self.assertEqual(kwargs["params"], {"term": "tvdb:12345"})

    @patch(PATCH_TARGET)
    async def test_no_match_returns_none(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, []))
        self.assertIsNone(await self.client.lookup_series_by_tvdb_id(999))

    @patch(PATCH_TARGET)
    async def test_non_200_returns_none(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertIsNone(await self.client.lookup_series_by_tvdb_id(1))


class TestSonarrAddSeries(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")
        self.series_data = {"title": "The Sopranos", "titleSlug": "the-sopranos", "images": [], "tvdbId": 1, "year": 1999}

    @patch(PATCH_TARGET)
    async def test_success_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"id": 5}))
        result = await self.client.add_series(self.series_data, quality_profile_id=1, root_folder_path="/tv")
        self.assertTrue(result["success"])

    @patch(PATCH_TARGET)
    async def test_success_201(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(201, {"id": 6}))
        result = await self.client.add_series(self.series_data, quality_profile_id=1, root_folder_path="/tv")
        self.assertTrue(result["success"])

    @patch(PATCH_TARGET)
    async def test_failure_status(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(400, text="Series already exists"))
        result = await self.client.add_series(self.series_data, quality_profile_id=1, root_folder_path="/tv")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Series already exists")

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        result = await self.client.add_series(self.series_data, quality_profile_id=1, root_folder_path="/tv")
        self.assertFalse(result["success"])
        self.assertIn("boom", result["error"])

    @patch(PATCH_TARGET)
    async def test_omits_language_profile_id_when_not_given(self, mock_ac):
        mock_instance = _mock_client(_response(200, {"id": 5}))
        mock_ac.return_value = mock_instance
        await self.client.add_series(self.series_data, quality_profile_id=1, root_folder_path="/tv")
        _, kwargs = mock_instance.post.call_args
        self.assertNotIn("languageProfileId", kwargs["json"])

    @patch(PATCH_TARGET)
    async def test_includes_language_profile_id_when_given(self, mock_ac):
        mock_instance = _mock_client(_response(200, {"id": 5}))
        mock_ac.return_value = mock_instance
        await self.client.add_series(
            self.series_data, quality_profile_id=1, root_folder_path="/tv", language_profile_id=2
        )
        _, kwargs = mock_instance.post.call_args
        self.assertEqual(kwargs["json"]["languageProfileId"], 2)


class TestSonarrDeleteSeries(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200))
        self.assertTrue(await self.client.delete_series(5))

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(404))
        self.assertFalse(await self.client.delete_series(5, delete_files=True))

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertFalse(await self.client.delete_series(5))


class TestSonarrGetSeries(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{"id": 1}]))
        self.assertEqual(await self.client.get_series(), [{"id": 1}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_series(), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_series(), [])


class TestSonarrTriggerCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(201, {"id": 99}))
        result = await self.client.trigger_command("SeriesSearch", seriesId=1)
        self.assertTrue(result["success"])

    @patch(PATCH_TARGET)
    async def test_failure(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(400, text="bad"))
        result = await self.client.trigger_command("SeriesSearch")
        self.assertFalse(result["success"])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        result = await self.client.trigger_command("SeriesSearch")
        self.assertFalse(result["success"])


class TestSonarrGetHistory(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"records": [{"id": 1}]}))
        self.assertEqual(await self.client.get_history(), [{"id": 1}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_history(page_size=10), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_history(), [])


class TestSonarrGetCalendar(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{"title": "Ep"}]))
        result = await self.client.get_calendar("2026-01-01", "2026-02-01")
        self.assertEqual(result, [{"title": "Ep"}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_calendar("2026-01-01", "2026-02-01"), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_calendar("2026-01-01", "2026-02-01"), [])


class TestSonarrGetQueue(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success_returns_records_and_includes_series_episode(self, mock_ac):
        mock_instance = _mock_client(_response(200, {"records": [{"id": 1, "series": {"title": "A Show"}}]}))
        mock_ac.return_value = mock_instance
        result = await self.client.get_queue()
        self.assertEqual(result, [{"id": 1, "series": {"title": "A Show"}}])
        _, kwargs = mock_instance.get.call_args
        self.assertEqual(kwargs["params"]["includeSeries"], "true")
        self.assertEqual(kwargs["params"]["includeEpisode"], "true")

    @patch(PATCH_TARGET)
    async def test_non_200_returns_empty(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_queue(), [])

    @patch(PATCH_TARGET)
    async def test_exception_returns_empty(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_queue(), [])


class TestSonarrGetHealth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_returns_checks(self, mock_ac):
        checks = [{"source": "DownloadClientCheck", "message": "Unable to communicate with SABnzbd."}]
        mock_ac.return_value = _mock_client(_response(200, checks))
        self.assertEqual(await self.client.get_health(), checks)

    @patch(PATCH_TARGET)
    async def test_unreadable_is_none(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertIsNone(await self.client.get_health())

    @patch(PATCH_TARGET)
    async def test_exception_is_none(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=Exception("refused"))
        self.assertIsNone(await self.client.get_health())


if __name__ == "__main__":
    unittest.main()


class TestSonarrEpisodeFiles(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_get_episodes_passes_series_id(self, mock_ac):
        mock = _mock_client(_response(200, [{"id": 1}]))
        mock_ac.return_value = mock
        self.assertEqual(await self.client.get_episodes(7), [{"id": 1}])
        self.assertEqual(mock.get.call_args.kwargs["params"], {"seriesId": 7})

    @patch(PATCH_TARGET)
    async def test_get_episodes_non_200_and_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_episodes(7), [])
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("down"))
        self.assertEqual(await self.client.get_episodes(7), [])

    @patch(PATCH_TARGET)
    async def test_delete_episode_file(self, mock_ac):
        mock = _mock_client(_response(200))
        mock_ac.return_value = mock
        self.assertTrue(await self.client.delete_episode_file(9))
        self.assertEqual(mock.delete.call_args.args[0], "http://sonarr/api/v3/episodefile/9")
        mock_ac.return_value = _mock_client(_response(404))
        self.assertFalse(await self.client.delete_episode_file(9))
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("down"))
        self.assertFalse(await self.client.delete_episode_file(9))


class TestSonarrSetEpisodesMonitored(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SonarrClient(api_url="http://sonarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_puts_episode_monitor(self, mock_ac):
        mock = _mock_client()
        mock.put.return_value = _response(202)
        mock_ac.return_value = mock
        self.assertTrue(await self.client.set_episodes_monitored([5]))
        self.assertEqual(mock.put.call_args.args[0], "http://sonarr/api/v3/episode/monitor")
        self.assertEqual(mock.put.call_args.kwargs["json"], {"episodeIds": [5], "monitored": True})

    @patch(PATCH_TARGET)
    async def test_failure_and_exception(self, mock_ac):
        mock = _mock_client()
        mock.put.return_value = _response(500)
        mock_ac.return_value = mock
        self.assertFalse(await self.client.set_episodes_monitored([5]))
        mock = _mock_client()
        mock.put.side_effect = RuntimeError("down")
        mock_ac.return_value = mock
        self.assertFalse(await self.client.set_episodes_monitored([5]))
