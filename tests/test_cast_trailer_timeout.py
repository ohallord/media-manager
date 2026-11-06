import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from media_manager.database import AsyncSessionLocal
import media_manager.web.api.integrations as integrations_module
from media_manager.web.api.integrations import _get_cast_and_trailer


class TestCastAndTrailerTimeout(unittest.TestCase):
    """
    Regression tests for the production incident where TMDb detail/person calls
    stalled (cluster DNS flakiness - see the ponytail comment in
    _get_cast_and_trailer) well past their own per-call httpx timeout, hanging
    /trending and /search indefinitely until the client got a non-JSON error
    page instead of a response. asyncio.wait_for's TimeoutError fires the same
    way whether the deadline actually elapses or the awaited call raises it
    directly, so mocking the raise keeps these tests fast.
    """

    def setUp(self):
        integrations_module.IN_MEMORY_CAST_CACHE.clear()

    def tearDown(self):
        integrations_module.IN_MEMORY_CAST_CACHE.clear()

    def test_detail_fetch_timeout_returns_without_cast_and_does_not_cache(self):
        client = MagicMock()
        client.get_movie_details = AsyncMock(side_effect=asyncio.TimeoutError)

        async def run():
            async with AsyncSessionLocal() as db:
                return await _get_cast_and_trailer(client, [999001], db, media_type="movie")

        result = asyncio.run(asyncio.wait_for(run(), timeout=10))

        self.assertEqual(result, {})
        self.assertNotIn(999001, integrations_module.IN_MEMORY_CAST_CACHE)

    def test_bio_fetch_timeout_keeps_cast_and_trailer_with_blank_bios(self):
        client = MagicMock()
        client.get_movie_details = AsyncMock(return_value={
            "credits": {"cast": [{"id": 5, "name": "Star", "character": "Hero", "profile_path": None}]},
            "videos": {"results": []}
        })
        client.get_person_details = AsyncMock(side_effect=asyncio.TimeoutError)
        client.extract_trailer_url = MagicMock(return_value=None)

        async def run():
            async with AsyncSessionLocal() as db:
                return await _get_cast_and_trailer(client, [999002], db, media_type="movie")

        result = asyncio.run(asyncio.wait_for(run(), timeout=10))

        self.assertIn(999002, result)
        self.assertEqual(result[999002]["cast"][0]["name"], "Star")
        self.assertEqual(result[999002]["cast"][0]["bio"], "")


class TestBoundedIdFetches(unittest.TestCase):
    def test_returns_empty_on_overall_timeout(self):
        async def hang(tid):
            await asyncio.sleep(30)
            return tid, ["x"]

        async def run():
            return await integrations_module._bounded_id_fetches(
                [1, 2], hang, timeout=0.15, concurrency=2, label="test"
            )

        result = asyncio.run(run())
        self.assertEqual(result, [])

    def test_skips_per_item_exceptions(self):
        async def one(tid):
            if tid == 1:
                raise RuntimeError("boom")
            return tid, "ok"

        async def run():
            return await integrations_module._bounded_id_fetches(
                [1, 2], one, timeout=2, label="test"
            )

        result = asyncio.run(run())
        self.assertEqual(result, [(2, "ok")])


if __name__ == "__main__":
    unittest.main()
