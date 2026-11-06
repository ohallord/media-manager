import asyncio
import logging
import queue
import unittest
from unittest.mock import patch

from sqlalchemy import delete, select

from media_manager import logger as logger_mod
from media_manager.database import SyncSessionLocal
from media_manager.models import AppLogEvent

MARK = "_ut_logger_marker_"


def _rows():
    session = SyncSessionLocal()
    try:
        return [r.message for r in session.execute(
            select(AppLogEvent).where(AppLogEvent.message.like(f"%{MARK}%"))).scalars()]
    finally:
        session.close()


class TestDbLogHandler(unittest.TestCase):
    def setUp(self):
        self.handler = logger_mod.PostgreSQLDBLogHandler()
        self.handler.setFormatter(logging.Formatter("%(message)s"))

    def tearDown(self):
        session = SyncSessionLocal()
        try:
            session.execute(delete(AppLogEvent).where(AppLogEvent.message.like(f"%{MARK}%")))
            session.commit()
        finally:
            session.close()

    def _record(self, msg, name="media_manager.test", level=logging.INFO):
        return logging.LogRecord(name, level, __file__, 1, msg, None, None)

    def test_rows_are_written_in_the_background_with_tags(self):
        self.handler.emit(self._record(f"[TRANSCODE_DONE] {MARK} a"))
        self.handler.emit(self._record(f"{MARK} b"))
        self.handler.flush()
        rows = sorted(_rows())
        self.assertEqual(rows, [f"[TRANSCODE_DONE] {MARK} a", f"{MARK} b"])
        session = SyncSessionLocal()
        try:
            tags = {r.event_tag for r in session.execute(
                select(AppLogEvent).where(AppLogEvent.message.like(f"%{MARK}%"))).scalars()}
        finally:
            session.close()
        self.assertEqual(tags, {"[TRANSCODE_DONE]", "[GENERAL]"})

    def test_request_loggers_are_not_persisted_but_still_broadcast(self):
        q = asyncio.Queue()
        logger_mod.sse_log_queues.add(q)
        try:
            self.handler.emit(self._record(f"{MARK} GET /x 200", name="uvicorn.access"))
            self.handler.emit(self._record(f"{MARK} HTTP Request", name="httpx"))
            self.handler.flush()
        finally:
            logger_mod.sse_log_queues.discard(q)
        self.assertEqual(_rows(), [])
        self.assertEqual(q.qsize(), 2)

    def test_emit_never_blocks_when_queue_is_full(self):
        with patch.object(self.handler._queue, "put_nowait", side_effect=queue.Full):
            self.handler.emit(self._record(f"{MARK} dropped"))  # must not raise or block
        self.handler.flush()
        self.assertEqual(_rows(), [])

    def test_write_failures_are_swallowed(self):
        with patch("media_manager.database.SyncSessionLocal") as factory:
            factory.return_value.commit.side_effect = RuntimeError("database is locked")
            self.handler._write_batch([("INFO", "[GENERAL]", f"{MARK} x", None)])
            factory.return_value.rollback.assert_called_once()
            factory.return_value.close.assert_called_once()

    def test_bad_record_goes_to_handle_error(self):
        with patch.object(self.handler, "format", side_effect=ValueError("bad")), \
             patch.object(self.handler, "handleError") as handle:
            self.handler.emit(self._record("x"))
        handle.assert_called_once()

    def test_flush_db_logs_flushes_root_handlers(self):
        root = logging.getLogger()
        root.addHandler(self.handler)
        try:
            self.handler.emit(self._record(f"{MARK} root"))
            logger_mod.flush_db_logs()
        finally:
            root.removeHandler(self.handler)
        self.assertEqual(_rows(), [f"{MARK} root"])


if __name__ == "__main__":
    unittest.main()
