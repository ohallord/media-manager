import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from media_manager.integrations.radarr import RadarrClient


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


PATCH_TARGET = "media_manager.integrations.radarr.httpx.AsyncClient"


class TestRadarrClientBasics(unittest.TestCase):
    def test_url_trailing_slash_stripped(self):
        client = RadarrClient(api_url="http://radarr:7878/", api_key="k")
        self.assertEqual(client.api_url, "http://radarr:7878")

    def test_headers(self):
        client = RadarrClient(api_url="http://radarr:7878", api_key="mykey")
        self.assertEqual(client._headers(), {"X-Api-Key": "mykey", "Content-Type": "application/json"})


class TestRadarrGetStatus(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")

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


class TestRadarrGetProfiles(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{"id": 1, "name": "HD"}]))
        self.assertEqual(await self.client.get_profiles(), [{"id": 1, "name": "HD"}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(404))
        self.assertEqual(await self.client.get_profiles(), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_profiles(), [])


class TestRadarrGetRootFolders(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{"path": "/movies"}]))
        self.assertEqual(await self.client.get_root_folders(), [{"path": "/movies"}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_root_folders(), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_root_folders(), [])


class TestRadarrGetHistory(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")

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


class TestRadarrSearchMovies(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{"title": "Fight Club"}]))
        self.assertEqual(await self.client.search_movies("Fight Club"), [{"title": "Fight Club"}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.search_movies("x"), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.search_movies("x"), [])


class TestRadarrLookupMovieByTmdbId(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success_dict(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"title": "Fight Club", "tmdbId": 550}))
        result = await self.client.lookup_movie_by_tmdb_id(550)
        self.assertEqual(result["tmdbId"], 550)

    @patch(PATCH_TARGET)
    async def test_success_non_dict_returns_none(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, ["not", "a", "dict"]))
        self.assertIsNone(await self.client.lookup_movie_by_tmdb_id(550))

    @patch(PATCH_TARGET)
    async def test_non_200_returns_none(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(404))
        self.assertIsNone(await self.client.lookup_movie_by_tmdb_id(550))

    @patch(PATCH_TARGET)
    async def test_exception_returns_none(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertIsNone(await self.client.lookup_movie_by_tmdb_id(550))


class TestRadarrAddMovie(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")
        self.movie_data = {"title": "Fight Club", "titleSlug": "fight-club", "images": [], "tmdbId": 550, "year": 1999}

    @patch(PATCH_TARGET)
    async def test_success_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"id": 5}))
        result = await self.client.add_movie(self.movie_data, quality_profile_id=1, root_folder_path="/movies")
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["id"], 5)

    @patch(PATCH_TARGET)
    async def test_success_201(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(201, {"id": 6}))
        result = await self.client.add_movie(self.movie_data, quality_profile_id=1, root_folder_path="/movies")
        self.assertTrue(result["success"])

    @patch(PATCH_TARGET)
    async def test_failure_status(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(400, text="Movie already exists"))
        result = await self.client.add_movie(self.movie_data, quality_profile_id=1, root_folder_path="/movies")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Movie already exists")

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        result = await self.client.add_movie(self.movie_data, quality_profile_id=1, root_folder_path="/movies")
        self.assertFalse(result["success"])
        self.assertIn("boom", result["error"])


class TestRadarrDeleteMovie(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200))
        self.assertTrue(await self.client.delete_movie(5))

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(404))
        self.assertFalse(await self.client.delete_movie(5, delete_files=True))

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertFalse(await self.client.delete_movie(5))


class TestRadarrGetMovies(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{"id": 1}]))
        self.assertEqual(await self.client.get_movies(), [{"id": 1}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_movies(), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_movies(), [])


class TestRadarrTriggerCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(201, {"id": 99}))
        result = await self.client.trigger_command("MoviesSearch", movieIds=[1, 2])
        self.assertTrue(result["success"])

    @patch(PATCH_TARGET)
    async def test_failure(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(400, text="bad"))
        result = await self.client.trigger_command("MoviesSearch")
        self.assertFalse(result["success"])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        result = await self.client.trigger_command("MoviesSearch")
        self.assertFalse(result["success"])
        self.assertIn("boom", result["error"])


class TestRadarrGetCalendar(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, [{"title": "Movie"}]))
        result = await self.client.get_calendar("2026-01-01", "2026-02-01")
        self.assertEqual(result, [{"title": "Movie"}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_calendar("2026-01-01", "2026-02-01"), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_calendar("2026-01-01", "2026-02-01"), [])


class TestRadarrGetQueue(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success_returns_records_and_includes_movie(self, mock_ac):
        mock_instance = _mock_client(_response(200, {"records": [{"id": 1, "movie": {"title": "A Movie"}}]}))
        mock_ac.return_value = mock_instance
        result = await self.client.get_queue()
        self.assertEqual(result, [{"id": 1, "movie": {"title": "A Movie"}}])
        _, kwargs = mock_instance.get.call_args
        self.assertEqual(kwargs["params"]["includeMovie"], "true")

    @patch(PATCH_TARGET)
    async def test_non_200_returns_empty(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        self.assertEqual(await self.client.get_queue(), [])

    @patch(PATCH_TARGET)
    async def test_exception_returns_empty(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        self.assertEqual(await self.client.get_queue(), [])


class TestRadarrQueueActions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_delete_queue_item_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200))
        self.assertTrue(await self.client.delete_queue_item(3))

    @patch(PATCH_TARGET)
    async def test_grab_queue_item_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(201))
        self.assertTrue(await self.client.grab_queue_item(3))


class TestRadarrGetHealth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")

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


class TestRadarrDeleteMovieFile(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_success_hits_moviefile_endpoint(self, mock_ac):
        mock = _mock_client(_response(200))
        mock_ac.return_value = mock
        self.assertTrue(await self.client.delete_movie_file(42))
        self.assertEqual(mock.delete.call_args.args[0], "http://radarr/api/v3/moviefile/42")

    @patch(PATCH_TARGET)
    async def test_non_200_is_false(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(404))
        self.assertFalse(await self.client.delete_movie_file(42))

    @patch(PATCH_TARGET)
    async def test_exception_is_false(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("down"))
        self.assertFalse(await self.client.delete_movie_file(42))


class TestRadarrSetMoviesMonitored(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RadarrClient(api_url="http://radarr", api_key="k")

    @patch(PATCH_TARGET)
    async def test_puts_movie_editor(self, mock_ac):
        mock = _mock_client()
        mock.put.return_value = _response(202)
        mock_ac.return_value = mock
        self.assertTrue(await self.client.set_movies_monitored([1, 2]))
        self.assertEqual(mock.put.call_args.args[0], "http://radarr/api/v3/movie/editor")
        self.assertEqual(mock.put.call_args.kwargs["json"], {"movieIds": [1, 2], "monitored": True})

    @patch(PATCH_TARGET)
    async def test_failure_and_exception(self, mock_ac):
        mock = _mock_client(_response(400))
        mock.put.return_value = _response(400)
        mock_ac.return_value = mock
        self.assertFalse(await self.client.set_movies_monitored([1]))
        mock = _mock_client()
        mock.put.side_effect = RuntimeError("down")
        mock_ac.return_value = mock
        self.assertFalse(await self.client.set_movies_monitored([1]))
