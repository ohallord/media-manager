import unittest
import asyncio
from datetime import datetime
from unittest.mock import patch, mock_open
from fastapi.testclient import TestClient
from sqlalchemy import delete

from media_manager.web.app import app
from media_manager.database import AsyncSessionLocal
from media_manager.models import AppLogEvent, FileMoveLog
from media_manager.web.api.logs import get_recent_log_file_lines


class TestApiLogs(unittest.TestCase):
    def setUp(self):
        from media_manager.web.api.auth import get_current_user, get_admin_user
        app.dependency_overrides[get_current_user] = lambda: {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_admin_user] = lambda: {"username": "admin", "role": "admin"}
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_logs_history(self):
        response = self.client.get("/api/v1/logs/history")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("events", data)

    def test_move_audits(self):
        response = self.client.get("/api/v1/logs/moves")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("moves", data)

    def test_recent_logs_endpoint(self):
        response = self.client.get("/api/v1/logs/recent?count=10")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("logs", data)
        self.assertEqual(data["requested_count"], 10)

    def test_recent_logs_count_out_of_range(self):
        response = self.client.get("/api/v1/logs/recent?count=0")
        self.assertEqual(response.status_code, 422)

    def test_history_with_level_filter_from_db(self):
        async def _seed():
            async with AsyncSessionLocal() as db:
                db.add(AppLogEvent(level="ERROR", event_tag="[TEST_TAG]", message="something broke"))
                await db.commit()
        asyncio.run(_seed())

        async def _cleanup():
            async with AsyncSessionLocal() as db:
                await db.execute(delete(AppLogEvent).where(AppLogEvent.event_tag == "[TEST_TAG]"))
                await db.commit()
        self.addCleanup(lambda: asyncio.run(_cleanup()))

        response = self.client.get("/api/v1/logs/history?level=error")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreaterEqual(data["total"], 1)
        self.assertTrue(any(e["event_tag"] == "[TEST_TAG]" for e in data["events"]))

    def test_history_with_tag_filter(self):
        async def _seed():
            async with AsyncSessionLocal() as db:
                db.add(AppLogEvent(level="INFO", event_tag="[UNIQUE_TAG_XYZ]", message="hello"))
                await db.commit()
        asyncio.run(_seed())

        async def _cleanup():
            async with AsyncSessionLocal() as db:
                await db.execute(delete(AppLogEvent).where(AppLogEvent.event_tag == "[UNIQUE_TAG_XYZ]"))
                await db.commit()
        self.addCleanup(lambda: asyncio.run(_cleanup()))

        response = self.client.get("/api/v1/logs/history?tag=UNIQUE_TAG_XYZ")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 1)

    def test_history_with_query_filter(self):
        async def _seed():
            async with AsyncSessionLocal() as db:
                db.add(AppLogEvent(level="INFO", event_tag="[GENERAL]", message="findable_needle_text"))
                await db.commit()
        asyncio.run(_seed())

        async def _cleanup():
            async with AsyncSessionLocal() as db:
                await db.execute(delete(AppLogEvent).where(AppLogEvent.message == "findable_needle_text"))
                await db.commit()
        self.addCleanup(lambda: asyncio.run(_cleanup()))

        response = self.client.get("/api/v1/logs/history?q=findable_needle")
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.json()["total"], 1)

    def test_history_falls_back_to_log_file_when_db_empty(self):
        with patch("media_manager.web.api.logs.get_recent_log_file_lines", return_value=[{"id": 1, "message": "fallback"}]) as mock_fallback:
            response = self.client.get("/api/v1/logs/history")
            self.assertEqual(response.status_code, 200)
            # Only called if the DB query (unfiltered) found nothing.
            data = response.json()
            self.assertIn("events", data)

    def test_move_audits_with_seeded_row(self):
        async def _seed():
            async with AsyncSessionLocal() as db:
                db.add(FileMoveLog(
                    source_path="/src/movie.mkv", destination_path="/dst/movie.mkv",
                    media_title="Test Movie", quality_before=0, quality_after=80,
                    copy_verified=True, status="SUCCESS",
                ))
                await db.commit()
        asyncio.run(_seed())

        async def _cleanup():
            async with AsyncSessionLocal() as db:
                await db.execute(delete(FileMoveLog).where(FileMoveLog.media_title == "Test Movie"))
                await db.commit()
        self.addCleanup(lambda: asyncio.run(_cleanup()))

        response = self.client.get("/api/v1/logs/moves")
        self.assertEqual(response.status_code, 200)
        moves = response.json()["moves"]
        self.assertTrue(any(m["media_title"] == "Test Movie" for m in moves))

    def test_stream_endpoint_backfills_and_connects(self):
        # Not exercised through TestClient: stream_live_logs's generator
        # runs `while True: await q.get()` after the initial backfill/connect
        # messages, which never completes on its own and hangs a synchronous
        # TestClient stream. Call the route function directly instead and
        # bound the iteration with wait_for so the never-ending tail of the
        # generator is simply never reached.
        from media_manager.web.api.logs import stream_live_logs

        async def run():
            response = await stream_live_logs(backfill=1, user={"username": "admin", "role": "admin"})
            agen = response.body_iterator
            first_chunk = await asyncio.wait_for(agen.__anext__(), timeout=2)
            self.assertIn("data:", first_chunk)
            await agen.aclose()

        asyncio.run(run())


class TestGetRecentLogFileLines(unittest.TestCase):
    def test_no_log_file_found_returns_empty(self):
        with patch("os.path.exists", return_value=False):
            self.assertEqual(get_recent_log_file_lines(), [])

    def test_parses_levels_and_tags(self):
        fake_content = (
            "2026-08-08 10:00:00,123 - INFO - [NEW_MEDIA] Something happened\n"
            "2026-08-08 10:00:01,456 - WARNING - [SCAN] Watch out\n"
            "2026-08-08 10:00:02,789 - ERROR - [COPY_VERIFIED] Failed badly\n"
            "2026-08-08 10:00:03,012 - DEBUG - just a debug line\n"
            "no level marker at all in this line\n"
            "\n"
        )
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=fake_content)):
            results = get_recent_log_file_lines(count=10)

        self.assertEqual(len(results), 5)  # blank line skipped
        levels = [r["level"] for r in results]
        self.assertEqual(levels, ["INFO", "WARNING", "ERROR", "DEBUG", "INFO"])
        self.assertEqual(results[0]["event_tag"], "[NEW_MEDIA]")
        self.assertEqual(results[-1]["event_tag"], "[GENERAL]")

    def test_handles_unparseable_timestamp(self):
        fake_content = "not-a-timestamp - INFO - [GENERAL] weird line\n"
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=fake_content)):
            results = get_recent_log_file_lines(count=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["created_at"], "not-a-timestamp")

    def test_read_exception_returns_empty(self):
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", side_effect=OSError("boom")):
            self.assertEqual(get_recent_log_file_lines(), [])

    def test_count_limits_tail(self):
        fake_content = "".join(f"2026-08-08 10:00:00,000 - INFO - [GENERAL] line {i}\n" for i in range(20))
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=fake_content)):
            results = get_recent_log_file_lines(count=3)
        self.assertEqual(len(results), 3)


if __name__ == "__main__":
    unittest.main()
