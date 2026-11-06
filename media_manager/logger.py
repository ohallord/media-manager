import os
import sys
import re
import json
import logging
import asyncio
import queue
import threading
import time
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from typing import Set

# Broadcaster queue list for live SSE log streaming
sse_log_queues: Set[asyncio.Queue] = set()

# Loggers that fire on every HTTP request in or out. They still go to the
# console and log file, but persisting them would mean a DB write per request.
NOT_PERSISTED_LOGGERS = ("uvicorn.access", "httpx", "httpcore")
DB_LOG_QUEUE_SIZE = 10000
DB_LOG_BATCH_SIZE = 200


class PostgreSQLDBLogHandler(logging.Handler):
    """
    Custom Logging Handler that writes structured log records to table
    app_log_events and broadcasts events to SSE subscribers.

    emit() never touches the database: rows are queued and written in batches
    by a background thread. A synchronous SQLite insert from the event loop
    blocked the whole web app for up to the 30 s busy timeout whenever another
    connection held the write lock (e.g. during a playback sync). If the queue
    is full, records are dropped from the DB (console/file still have them).
    """
    def __init__(self):
        super().__init__()
        self._queue: "queue.Queue" = queue.Queue(maxsize=DB_LOG_QUEUE_SIZE)
        self._writer = threading.Thread(target=self._write_forever, name="db-log-writer", daemon=True)
        self._writer.start()

    def emit(self, record):
        try:
            msg = self.format(record)
            # Detect tag pattern like [NEW_MEDIA], [COPY_VERIFIED], [FILEBOT_MOVED]
            tag_match = re.search(r'(\[[A-Z_]+\])', msg)
            event_tag = tag_match.group(1) if tag_match else "[GENERAL]"
            
            # Broadcast to active SSE listeners
            log_payload = {
                "level": record.levelname,
                "event_tag": event_tag,
                "message": msg,
                "created_at": datetime.utcnow().isoformat() + "Z"
            }
            
            for q in list(sse_log_queues):
                try:
                    q.put_nowait(log_payload)
                except Exception:
                    pass

            if record.name.startswith(NOT_PERSISTED_LOGGERS):
                return
            try:
                self._queue.put_nowait((record.levelname, event_tag, msg, datetime.utcnow()))
            except queue.Full:
                pass
        except Exception:
            self.handleError(record)

    def _write_forever(self):
        while True:
            batch = [self._queue.get()]
            while len(batch) < DB_LOG_BATCH_SIZE:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._write_batch(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _write_batch(self, batch):
        from media_manager.database import SyncSessionLocal
        from media_manager.models import AppLogEvent
        session = SyncSessionLocal()
        try:
            session.add_all([AppLogEvent(level=level, event_tag=tag, message=msg, created_at=created)
                             for level, tag, msg, created in batch])
            session.commit()
        except Exception:
            session.rollback()  # never retry forever or raise from the writer thread
        finally:
            session.close()

    def flush(self, timeout: float = 5.0):
        """Waits (bounded) until queued records are written - for tests and shutdown."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)


def flush_db_logs(timeout: float = 5.0) -> None:
    for handler in logging.getLogger().handlers:
        if isinstance(handler, PostgreSQLDBLogHandler):
            handler.flush(timeout)

def setup_logging(log_level=logging.INFO):
    """
    Sets up logging with console output, TimedRotatingFileHandler,
    and PostgreSQL DB Log Handler.
    """
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_file = os.path.join(base_dir, "media_manager.log")

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    if root_logger.handlers:
        return root_logger

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # Stream Handler (console / stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Timed Rotating File Handler (rotates daily, retains 90 days)
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=90,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.suffix = "%Y-%m-%d"
    root_logger.addHandler(file_handler)

    # DB Handler
    db_handler = PostgreSQLDBLogHandler()
    db_handler.setFormatter(formatter)
    root_logger.addHandler(db_handler)

    return root_logger
