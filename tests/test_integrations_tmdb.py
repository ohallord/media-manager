import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from media_manager.integrations.tmdb import TMDbClient


def _mock_client(responses=None, raise_exc=None):
    """responses: either a single response or a list consumed in order (for paged calls)."""
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


PATCH_TARGET = "media_manager.integrations.tmdb.httpx.AsyncClient"


class TestTMDbClientInit(unittest.TestCase):
    def test_v3_api_key_uses_params(self):
        client = TMDbClient(api_key="abc123")
        self.assertEqual(client.params, {"api_key": "abc123"})
        self.assertNotIn("Authorization", client.headers)

    def test_v4_bearer_token_uses_header(self):
        client = TMDbClient(api_key="eyJhbGciOiJIUzI1NiJ9.abc.def")  # gitleaks:allow (fake JWT)
        self.assertEqual(client.headers["Authorization"], "Bearer eyJhbGciOiJIUzI1NiJ9.abc.def")  # gitleaks:allow (fake JWT)
        self.assertEqual(client.params, {})

    def test_default_url(self):
        client = TMDbClient(api_url="", api_key="k")
        self.assertEqual(client.api_url, "https://api.themoviedb.org/3")

    def test_custom_url_trailing_slash_stripped(self):
        client = TMDbClient(api_url="https://custom/3/", api_key="k")
        self.assertEqual(client.api_url, "https://custom/3")


class TestTMDbGetStatus(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertFalse(await client.get_status())

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200))
        client = TMDbClient(api_key="k")
        self.assertTrue(await client.get_status())

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(401))
        client = TMDbClient(api_key="k")
        self.assertFalse(await client.get_status())

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        self.assertFalse(await client.get_status())


class TestTMDbSearchMovie(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertEqual(await client.search_movie("Fight Club"), [])

    @patch(PATCH_TARGET)
    async def test_success_no_year(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"results": [{"title": "Fight Club"}]}))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.search_movie("Fight Club"), [{"title": "Fight Club"}])

    @patch(PATCH_TARGET)
    async def test_success_with_year(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"results": [{"title": "Fight Club"}]}))
        client = TMDbClient(api_key="k")
        result = await client.search_movie("Fight Club", year=1999)
        self.assertEqual(result, [{"title": "Fight Club"}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.search_movie("x"), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.search_movie("x"), [])


class TestTMDbSearchTv(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertEqual(await client.search_tv("The Sopranos"), [])

    @patch(PATCH_TARGET)
    async def test_success_with_year(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"results": [{"name": "The Sopranos"}]}))
        client = TMDbClient(api_key="k")
        result = await client.search_tv("The Sopranos", year=1999)
        self.assertEqual(result, [{"name": "The Sopranos"}])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.search_tv("x"), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.search_tv("x"), [])


class TestTMDbGetMovieDetails(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertIsNone(await client.get_movie_details(550))

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"id": 550, "title": "Fight Club"}))
        client = TMDbClient(api_key="k")
        result = await client.get_movie_details(550)
        self.assertEqual(result["id"], 550)

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(404))
        client = TMDbClient(api_key="k")
        self.assertIsNone(await client.get_movie_details(550))

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        self.assertIsNone(await client.get_movie_details(550))


class TestTMDbExtractTrailerUrl(unittest.TestCase):
    def test_none_details(self):
        self.assertIsNone(TMDbClient.extract_trailer_url(None))

    def test_no_videos_key(self):
        self.assertIsNone(TMDbClient.extract_trailer_url({}))

    def test_no_youtube_videos(self):
        details = {"videos": {"results": [{"site": "Vimeo", "key": "abc", "type": "Trailer"}]}}
        self.assertIsNone(TMDbClient.extract_trailer_url(details))

    def test_prefers_official_trailer(self):
        details = {"videos": {"results": [
            {"site": "YouTube", "key": "unofficial", "type": "Trailer", "official": False},
            {"site": "YouTube", "key": "official1", "type": "Trailer", "official": True},
        ]}}
        url = TMDbClient.extract_trailer_url(details)
        self.assertEqual(url, "https://www.youtube.com/watch?v=official1")

    def test_falls_back_to_unofficial_trailer(self):
        details = {"videos": {"results": [
            {"site": "YouTube", "key": "unofficial", "type": "Trailer", "official": False},
        ]}}
        url = TMDbClient.extract_trailer_url(details)
        self.assertEqual(url, "https://www.youtube.com/watch?v=unofficial")

    def test_falls_back_to_teaser(self):
        details = {"videos": {"results": [
            {"site": "YouTube", "key": "teaser1", "type": "Teaser", "official": False},
        ]}}
        url = TMDbClient.extract_trailer_url(details)
        self.assertEqual(url, "https://www.youtube.com/watch?v=teaser1")

    def test_no_matching_type_returns_none(self):
        details = {"videos": {"results": [
            {"site": "YouTube", "key": "bts", "type": "Behind the Scenes", "official": False},
        ]}}
        self.assertIsNone(TMDbClient.extract_trailer_url(details))

    def test_video_missing_key_filtered_out(self):
        details = {"videos": {"results": [
            {"site": "YouTube", "key": "", "type": "Trailer", "official": True},
        ]}}
        self.assertIsNone(TMDbClient.extract_trailer_url(details))


class TestTMDbGetTvDetails(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertIsNone(await client.get_tv_details(1))

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"id": 1, "name": "The Sopranos"}))
        client = TMDbClient(api_key="k")
        result = await client.get_tv_details(1)
        self.assertEqual(result["name"], "The Sopranos")

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(404))
        client = TMDbClient(api_key="k")
        self.assertIsNone(await client.get_tv_details(1))

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        self.assertIsNone(await client.get_tv_details(1))


class TestTMDbGetMovieGenres(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertEqual(await client.get_movie_genres(), {})

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"genres": [{"id": 28, "name": "Action"}]}))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.get_movie_genres(), {28: "Action"})

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.get_movie_genres(), {})

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.get_movie_genres(), {})


class TestTMDbDiscoverUpcomingMovies(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertEqual(await client.discover_upcoming_movies("2026-01-01", "2026-02-01"), [])

    @patch(PATCH_TARGET)
    async def test_single_page(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"results": [{"title": "A"}], "total_pages": 1}))
        client = TMDbClient(api_key="k")
        result = await client.discover_upcoming_movies("2026-01-01", "2026-02-01")
        self.assertEqual(result, [{"title": "A"}])

    @patch(PATCH_TARGET)
    async def test_multi_page_stops_at_total_pages(self, mock_ac):
        page1 = _response(200, {"results": [{"title": "A"}], "total_pages": 2})
        page2 = _response(200, {"results": [{"title": "B"}], "total_pages": 2})
        mock_ac.return_value = _mock_client([page1, page2])
        client = TMDbClient(api_key="k")
        result = await client.discover_upcoming_movies("2026-01-01", "2026-02-01", max_pages=10)
        self.assertEqual(result, [{"title": "A"}, {"title": "B"}])

    @patch(PATCH_TARGET)
    async def test_stops_on_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        client = TMDbClient(api_key="k")
        result = await client.discover_upcoming_movies("2026-01-01", "2026-02-01")
        self.assertEqual(result, [])

    @patch(PATCH_TARGET)
    async def test_exception_returns_partial_results(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        result = await client.discover_upcoming_movies("2026-01-01", "2026-02-01")
        self.assertEqual(result, [])

    @patch(PATCH_TARGET)
    async def test_max_pages_respected(self, mock_ac):
        page = _response(200, {"results": [{"title": "A"}], "total_pages": 99})
        mock_ac.return_value = _mock_client([page, page])
        client = TMDbClient(api_key="k")
        result = await client.discover_upcoming_movies("2026-01-01", "2026-02-01", max_pages=2)
        self.assertEqual(len(result), 2)


class TestTMDbGetTvEpisodeDetails(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertIsNone(await client.get_tv_episode_details(1, 1, 1))

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"name": "Pilot"}))
        client = TMDbClient(api_key="k")
        result = await client.get_tv_episode_details(1, 1, 1)
        self.assertEqual(result["name"], "Pilot")

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(404))
        client = TMDbClient(api_key="k")
        self.assertIsNone(await client.get_tv_episode_details(1, 1, 1))

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        self.assertIsNone(await client.get_tv_episode_details(1, 1, 1))


class TestTMDbGetPersonDetails(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertIsNone(await client.get_person_details(1))

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"name": "Brad Pitt"}))
        client = TMDbClient(api_key="k")
        result = await client.get_person_details(1)
        self.assertEqual(result["name"], "Brad Pitt")

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(404))
        client = TMDbClient(api_key="k")
        self.assertIsNone(await client.get_person_details(1))

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        self.assertIsNone(await client.get_person_details(1))


class TestTMDbFindTvByTvdb(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertIsNone(await client.find_tv_tmdb_id_by_tvdb(121361))

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"tv_results": [{"id": 1396}]}))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.find_tv_tmdb_id_by_tvdb(121361), 1396)

    @patch(PATCH_TARGET)
    async def test_empty_results(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"tv_results": []}))
        client = TMDbClient(api_key="k")
        self.assertIsNone(await client.find_tv_tmdb_id_by_tvdb(1))


class TestTMDbGetTvWatchProviders(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertEqual(await client.get_tv_watch_providers(1), [])

    @patch(PATCH_TARGET)
    async def test_success_extracts_us_flatrate_names(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {
            "results": {
                "US": {"flatrate": [{"provider_name": "Netflix"}, {"provider_name": "Hulu"}]},
                "GB": {"flatrate": [{"provider_name": "BBC iPlayer"}]}
            }
        }))
        client = TMDbClient(api_key="k")
        result = await client.get_tv_watch_providers(1)
        self.assertEqual(result, ["Netflix", "Hulu"])

    @patch(PATCH_TARGET)
    async def test_no_us_entry_returns_empty(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"results": {"GB": {"flatrate": [{"provider_name": "BBC iPlayer"}]}}}))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.get_tv_watch_providers(1), [])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(404))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.get_tv_watch_providers(1), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.get_tv_watch_providers(1), [])


class TestTMDbGetTvExternalIds(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertIsNone(await client.get_tv_external_ids(1))

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"tvdb_id": 12345, "imdb_id": "tt123"}))
        client = TMDbClient(api_key="k")
        result = await client.get_tv_external_ids(1)
        self.assertEqual(result["tvdb_id"], 12345)

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(404))
        client = TMDbClient(api_key="k")
        self.assertIsNone(await client.get_tv_external_ids(1))

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        self.assertIsNone(await client.get_tv_external_ids(1))


class TestTMDbGetTvGenres(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertEqual(await client.get_tv_genres(), {})

    @patch(PATCH_TARGET)
    async def test_success(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"genres": [{"id": 10759, "name": "Action & Adventure"}]}))
        client = TMDbClient(api_key="k")
        result = await client.get_tv_genres()
        self.assertEqual(result, {10759: "Action & Adventure"})

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.get_tv_genres(), {})

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.get_tv_genres(), {})


class TestTMDbDiscoverUpcomingTv(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertEqual(await client.discover_upcoming_tv("2026-01-01", "2026-02-01"), [])

    @patch(PATCH_TARGET)
    async def test_single_page(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {"results": [{"name": "A"}], "total_pages": 1}))
        client = TMDbClient(api_key="k")
        result = await client.discover_upcoming_tv("2026-01-01", "2026-02-01")
        self.assertEqual(result, [{"name": "A"}])

    @patch(PATCH_TARGET)
    async def test_multi_page_stops_at_total_pages(self, mock_ac):
        page1 = _response(200, {"results": [{"name": "A"}], "total_pages": 2})
        page2 = _response(200, {"results": [{"name": "B"}], "total_pages": 2})
        mock_ac.return_value = _mock_client([page1, page2])
        client = TMDbClient(api_key="k")
        result = await client.discover_upcoming_tv("2026-01-01", "2026-02-01", max_pages=10)
        self.assertEqual(result, [{"name": "A"}, {"name": "B"}])

    @patch(PATCH_TARGET)
    async def test_stops_on_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        client = TMDbClient(api_key="k")
        result = await client.discover_upcoming_tv("2026-01-01", "2026-02-01")
        self.assertEqual(result, [])

    @patch(PATCH_TARGET)
    async def test_exception_returns_partial_results(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        result = await client.discover_upcoming_tv("2026-01-01", "2026-02-01")
        self.assertEqual(result, [])


class TestTMDbGetTrending(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertEqual(await client.get_trending(), [])

    @patch(PATCH_TARGET)
    async def test_single_page_uses_media_type_and_window_in_url(self, mock_ac):
        mock_instance = _mock_client(_response(200, {"results": [{"title": "A"}], "total_pages": 1}))
        mock_ac.return_value = mock_instance
        client = TMDbClient(api_key="k")
        result = await client.get_trending(media_type="tv", time_window="day")
        self.assertEqual(result, [{"title": "A"}])
        args, _ = mock_instance.get.call_args
        self.assertTrue(args[0].endswith("/trending/tv/day"))

    @patch(PATCH_TARGET)
    async def test_multi_page_stops_at_total_pages(self, mock_ac):
        page1 = _response(200, {"results": [{"title": "A"}], "total_pages": 2})
        page2 = _response(200, {"results": [{"title": "B"}], "total_pages": 2})
        mock_ac.return_value = _mock_client([page1, page2])
        client = TMDbClient(api_key="k")
        result = await client.get_trending(max_pages=10)
        self.assertEqual(result, [{"title": "A"}, {"title": "B"}])

    @patch(PATCH_TARGET)
    async def test_stops_on_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.get_trending(), [])

    @patch(PATCH_TARGET)
    async def test_exception_returns_partial_results(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.get_trending(), [])


class TestTMDbGetMovieWatchProviders(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key(self):
        client = TMDbClient(api_key="")
        self.assertEqual(await client.get_movie_watch_providers(1), [])

    @patch(PATCH_TARGET)
    async def test_success_extracts_us_flatrate_names(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(200, {
            "results": {"US": {"flatrate": [{"provider_name": "Netflix"}]}}
        }))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.get_movie_watch_providers(1), ["Netflix"])

    @patch(PATCH_TARGET)
    async def test_non_200(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(404))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.get_movie_watch_providers(1), [])

    @patch(PATCH_TARGET)
    async def test_exception(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.get_movie_watch_providers(1), [])


class TestTMDbRelatedTitles(unittest.IsolatedAsyncioTestCase):
    """get_movie_recommendations / get_similar_movies / get_tv_recommendations / get_similar_tv"""

    async def test_no_api_key_all_return_empty(self):
        client = TMDbClient(api_key="")
        self.assertEqual(await client.get_movie_recommendations(1), [])
        self.assertEqual(await client.get_similar_movies(1), [])
        self.assertEqual(await client.get_tv_recommendations(1), [])
        self.assertEqual(await client.get_similar_tv(1), [])

    @patch(PATCH_TARGET)
    async def test_movie_recommendations_hits_correct_url(self, mock_ac):
        mock_instance = _mock_client(_response(200, {"results": [{"id": 2, "title": "Rec"}]}))
        mock_ac.return_value = mock_instance
        client = TMDbClient(api_key="k")
        result = await client.get_movie_recommendations(1)
        self.assertEqual(result, [{"id": 2, "title": "Rec"}])
        args, _ = mock_instance.get.call_args
        self.assertTrue(args[0].endswith("/movie/1/recommendations"))

    @patch(PATCH_TARGET)
    async def test_similar_movies_hits_correct_url(self, mock_ac):
        mock_instance = _mock_client(_response(200, {"results": [{"id": 3, "title": "Sim"}]}))
        mock_ac.return_value = mock_instance
        client = TMDbClient(api_key="k")
        result = await client.get_similar_movies(1)
        self.assertEqual(result, [{"id": 3, "title": "Sim"}])
        args, _ = mock_instance.get.call_args
        self.assertTrue(args[0].endswith("/movie/1/similar"))

    @patch(PATCH_TARGET)
    async def test_tv_recommendations_hits_correct_url(self, mock_ac):
        mock_instance = _mock_client(_response(200, {"results": [{"id": 4, "name": "Rec Show"}]}))
        mock_ac.return_value = mock_instance
        client = TMDbClient(api_key="k")
        result = await client.get_tv_recommendations(1)
        self.assertEqual(result, [{"id": 4, "name": "Rec Show"}])
        args, _ = mock_instance.get.call_args
        self.assertTrue(args[0].endswith("/tv/1/recommendations"))

    @patch(PATCH_TARGET)
    async def test_similar_tv_hits_correct_url(self, mock_ac):
        mock_instance = _mock_client(_response(200, {"results": [{"id": 5, "name": "Sim Show"}]}))
        mock_ac.return_value = mock_instance
        client = TMDbClient(api_key="k")
        result = await client.get_similar_tv(1)
        self.assertEqual(result, [{"id": 5, "name": "Sim Show"}])
        args, _ = mock_instance.get.call_args
        self.assertTrue(args[0].endswith("/tv/1/similar"))

    @patch(PATCH_TARGET)
    async def test_non_200_returns_empty(self, mock_ac):
        mock_ac.return_value = _mock_client(_response(500))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.get_movie_recommendations(1), [])

    @patch(PATCH_TARGET)
    async def test_exception_returns_empty(self, mock_ac):
        mock_ac.return_value = _mock_client(raise_exc=RuntimeError("boom"))
        client = TMDbClient(api_key="k")
        self.assertEqual(await client.get_similar_tv(1), [])


if __name__ == "__main__":
    unittest.main()
