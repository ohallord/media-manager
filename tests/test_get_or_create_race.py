"""Singleton rows must survive concurrent first requests.

The H.265 page fires status, dry-run and candidates at once. On a fresh
install each request found no `transcode_control` row, all tried to insert
id='global', and the losers returned 500 (UNIQUE constraint failed). The
cleanup page has the same pattern for `cleanup_settings`.

These tests make one request lose the race deterministically: the row is
created by "another request" right after our SELECT saw nothing.
"""
import asyncio
import unittest

from sqlalchemy import delete

from media_manager.database import AsyncSessionLocal
from media_manager.models import CleanupSettings, TranscodeControl
from media_manager.web.services import cleanup_suggestions, transcode_planner

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


class _EmptyResult:
    def scalar_one_or_none(self):
        return None


def lose_race_on_first_select(db, create_competitor):
    """Wrap db.execute so the first SELECT sees no row, but a competing
    request inserts and commits it before we do."""
    real_execute = db.execute
    state = {"first": True}

    async def execute(stmt, *a, **kw):
        if state["first"]:
            state["first"] = False
            await create_competitor()
            return _EmptyResult()
        return await real_execute(stmt, *a, **kw)

    db.execute = execute


async def _clear():
    async with AsyncSessionLocal() as db:
        await db.execute(delete(TranscodeControl))
        await db.execute(delete(CleanupSettings))
        await db.commit()


class TestTranscodeControlRace(unittest.TestCase):
    def setUp(self):
        _run(_clear())

    def tearDown(self):
        _run(_clear())

    def test_loser_of_insert_race_returns_existing_row(self):
        async def competitor():
            async with AsyncSessionLocal() as other:
                other.add(TranscodeControl(id="global", mode="pilot"))
                await other.commit()

        async def scenario():
            async with AsyncSessionLocal() as db:
                lose_race_on_first_select(db, competitor)
                row = await transcode_planner.get_or_create_control(db)
                return row.id, row.mode

        self.assertEqual(_run(scenario()), ("global", "pilot"))

    def test_concurrent_first_requests_all_succeed(self):
        async def one():
            async with AsyncSessionLocal() as db:
                return (await transcode_planner.get_or_create_control(db)).id

        async def many():
            return await asyncio.gather(*(one() for _ in range(8)))

        self.assertEqual(set(_run(many())), {"global"})


class TestCleanupSettingsRace(unittest.TestCase):
    def setUp(self):
        _run(_clear())

    def tearDown(self):
        _run(_clear())

    def test_loser_of_insert_race_returns_existing_row(self):
        async def competitor():
            async with AsyncSessionLocal() as other:
                other.add(CleanupSettings(media_type="movie", never_played_grace_days=11, stale_after_days=22))
                await other.commit()

        async def scenario():
            async with AsyncSessionLocal() as db:
                lose_race_on_first_select(db, competitor)
                row = await cleanup_suggestions.get_or_create_settings(db, "movie")
                return row.media_type, row.never_played_grace_days

        self.assertEqual(_run(scenario()), ("movie", 11))


if __name__ == "__main__":
    unittest.main()
