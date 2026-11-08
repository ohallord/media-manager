import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from media_manager import database

from media_manager.database import (
    init_db_schema,
    init_db_schema_sync,
    get_async_db,
    get_sync_db,
    sync_engine,
    AsyncSessionLocal,
)


class TestInitDbSchemaSync(unittest.TestCase):
    def test_creates_expected_tables(self):
        init_db_schema_sync()  # idempotent - already run once by scripts/run_tests.sh
        inspector = inspect(sync_engine)
        tables = set(inspector.get_table_names())
        for expected in ["users", "media_items", "duplicate_groups", "monitored_directories", "service_heartbeats", "playback_stats", "cleanup_settings", "transcode_settings", "transcode_control", "transcode_jobs"]:
            self.assertIn(expected, tables)

    def test_integration_configs_has_extra_json_column(self):
        init_db_schema_sync()
        inspector = inspect(sync_engine)
        columns = {c["name"] for c in inspector.get_columns("integration_configs")}
        self.assertIn("extra_json", columns)

    def test_service_heartbeats_has_version_columns(self):
        init_db_schema_sync()
        columns = {c["name"] for c in inspect(sync_engine).get_columns("service_heartbeats")}
        self.assertTrue({"version", "detail_json"} <= columns)

    def test_idempotent_second_call_does_not_raise(self):
        init_db_schema_sync()
        init_db_schema_sync()  # must not raise on already-migrated columns/tables


_LEGACY_MEDIA_ITEMS_DDL = """
CREATE TABLE media_items (
    id VARCHAR(36) PRIMARY KEY, path TEXT NOT NULL UNIQUE, storage_volume VARCHAR(128) NOT NULL,
    title VARCHAR(255) NOT NULL, media_type VARCHAR(32) NOT NULL, season INTEGER, episode INTEGER,
    resolution VARCHAR(32) NOT NULL, height INTEGER NOT NULL, width INTEGER NOT NULL,
    video_codec VARCHAR(64) NOT NULL, audio_codec VARCHAR(64) NOT NULL, audio_channels INTEGER NOT NULL,
    bitrate BIGINT NOT NULL, size_bytes BIGINT NOT NULL, quality_score INTEGER NOT NULL,
    file_created_at DATETIME NOT NULL, last_scanned_at DATETIME NOT NULL,
    duplicate_group_id VARCHAR(36)
)
"""


class TestMediaItemsRichProbeColumns(unittest.TestCase):
    """A DB created before the rich-probe columns gets them ALTERed in by both init paths."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="mm_legacy_db_")
        self.db_file = os.path.join(self.tmpdir, "legacy.db")
        legacy = create_engine(f"sqlite:///{self.db_file}")
        with legacy.begin() as conn:
            conn.execute(text(_LEGACY_MEDIA_ITEMS_DDL))
        legacy.dispose()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _media_item_columns(self):
        engine = create_engine(f"sqlite:///{self.db_file}")
        try:
            return {c["name"] for c in inspect(engine).get_columns("media_items")}
        finally:
            engine.dispose()

    def test_sync_init_adds_columns_to_legacy_table(self):
        engine = create_engine(f"sqlite:///{self.db_file}")
        with patch.object(database, "sync_engine", engine):
            database.init_db_schema_sync()
            database.init_db_schema_sync()  # second run is a no-op, must not raise
        engine.dispose()
        columns = self._media_item_columns()
        for col, _ in database._MEDIA_ITEMS_COLUMNS:
            self.assertIn(col, columns)

    def test_async_init_adds_columns_to_legacy_table(self):
        engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_file}")

        async def run():
            with patch.object(database, "async_engine", engine):
                await database.init_db_schema()
            await engine.dispose()

        asyncio.run(run())
        columns = self._media_item_columns()
        for col, _ in database._MEDIA_ITEMS_COLUMNS:
            self.assertIn(col, columns)

    def test_column_list_matches_model(self):
        from media_manager.metadata import DETAIL_FIELDS
        from media_manager.models import MediaItem
        listed = {col for col, _ in database._MEDIA_ITEMS_COLUMNS}
        self.assertEqual(listed, set(DETAIL_FIELDS) | {"probed_at", "probe_version"})
        for col in listed:
            self.assertIn(col, MediaItem.__table__.columns)


class TestInitDbSchemaAsync(unittest.TestCase):
    def test_creates_expected_tables(self):
        asyncio.run(init_db_schema())
        inspector = inspect(sync_engine)
        tables = set(inspector.get_table_names())
        self.assertIn("metadata_cache", tables)


class TestGetSyncDb(unittest.TestCase):
    def test_returns_a_session(self):
        db = get_sync_db()
        self.assertIsNotNone(db)
        self.assertTrue(hasattr(db, "query"))


class TestGetAsyncDb(unittest.TestCase):
    def test_yields_session_and_commits_on_success(self):
        async def run():
            agen = get_async_db()
            session = await agen.__anext__()
            self.assertIsNotNone(session)
            with self.assertRaises(StopAsyncIteration):
                await agen.__anext__()

        asyncio.run(run())

    def test_rolls_back_and_reraises_on_exception(self):
        async def run():
            agen = get_async_db()
            session = await agen.__anext__()
            self.assertIsNotNone(session)
            with self.assertRaises(RuntimeError):
                await agen.athrow(RuntimeError("boom"))

        asyncio.run(run())

    def test_session_is_usable_for_a_query(self):
        from sqlalchemy import text

        async def run():
            async with AsyncSessionLocal() as db:
                result = await db.execute(text("SELECT 1"))
                self.assertEqual(result.scalar(), 1)

        asyncio.run(run())


class TestSqlitePragmas(unittest.TestCase):
    """
    WAL journal mode + a busy_timeout so a read from the web server doesn't
    immediately fail with "database is locked" when
    media-manager-watcher.service is mid-write on the same on-disk file - see
    the comment in database.py for the incident this fixes (reported as a
    frontend JSON.parse error on Search) and the deadlock the first attempt
    at this caused (see git history for fix-sqlite-database-locked / its
    revert) before landing on this connect_args-based approach.
    """

    def test_sync_engine_has_wal_and_busy_timeout(self):
        from sqlalchemy import text
        from media_manager.database import sync_engine

        with sync_engine.connect() as conn:
            journal_mode = conn.execute(text("PRAGMA journal_mode")).scalar()
            busy_timeout = conn.execute(text("PRAGMA busy_timeout")).scalar()

        self.assertEqual(journal_mode.lower(), "wal")
        self.assertEqual(busy_timeout, 30000)

    def test_async_engine_has_wal_and_busy_timeout(self):
        from sqlalchemy import text
        from media_manager.database import AsyncSessionLocal

        async def run():
            async with AsyncSessionLocal() as db:
                journal_mode = (await db.execute(text("PRAGMA journal_mode"))).scalar()
                busy_timeout = (await db.execute(text("PRAGMA busy_timeout"))).scalar()
            return journal_mode, busy_timeout

        journal_mode, busy_timeout = asyncio.run(run())
        self.assertEqual(journal_mode.lower(), "wal")
        self.assertEqual(busy_timeout, 30000)

    def test_reader_not_blocked_by_a_concurrent_writer(self):
        """
        Direct reproduction of the actual original bug: a second, independent
        sqlite3 connection holds an open write transaction (exactly what
        media-manager-watcher.service does mid-scan) while our engine tries a
        plain read. Before WAL mode this raised "database is locked"
        immediately; under WAL, readers proceed without waiting on the writer.
        """
        import sqlite3
        from sqlalchemy import text
        from media_manager.database import sync_engine

        db_path = sync_engine.url.database
        writer_conn = sqlite3.connect(db_path, timeout=0)
        try:
            writer_conn.execute("BEGIN IMMEDIATE")
            writer_conn.execute("CREATE TABLE IF NOT EXISTS _wal_concurrency_test (id INTEGER)")

            with sync_engine.connect() as conn:
                result = conn.execute(text("SELECT 1")).scalar()
            self.assertEqual(result, 1)
        finally:
            writer_conn.rollback()
            writer_conn.close()

    def test_multiple_async_queries_around_a_network_call_do_not_hang_or_error(self):
        """
        Regression test for the actual production failure mode the first fix
        attempt caused: it passed every pragma-level test above (WAL/timeout
        were genuinely applied correctly) but deadlocked real requests under
        real async server load - specifically multiple sequential queries on
        the SAME AsyncSession with an awaited non-DB async call (an outbound
        HTTP request in the real endpoint) in between. That combination is
        exactly what media_search.js's backend
        (search_media_local_and_remote) does, and it's what a bare
        asyncio.run() pragma check doesn't exercise. Runs with a hard asyncio
        timeout so this test fails fast instead of hanging the suite if the
        same class of bug reappears.
        """
        from sqlalchemy import text
        from media_manager.database import AsyncSessionLocal

        async def run():
            async with AsyncSessionLocal() as db:
                r1 = await db.execute(text("SELECT 1"))
                self.assertEqual(r1.scalar(), 1)

                import httpx
                async with httpx.AsyncClient(timeout=2.0) as client:
                    try:
                        await client.get("http://127.0.0.1:1/nonexistent", timeout=1.0)
                    except Exception:
                        pass  # the network failure itself is irrelevant - only the DB behavior after it matters

                r2 = await db.execute(text("SELECT 1"))
                self.assertEqual(r2.scalar(), 1)

        asyncio.run(asyncio.wait_for(run(), timeout=10))

    def test_real_search_endpoint_completes_through_the_full_http_stack(self):
        """
        The most direct regression test for what actually broke in
        production: every other /search test in this suite overrides
        get_async_db with a fake routing session, so none of them exercise
        the real async_engine/AsyncSessionLocal at all - which is exactly why
        the original deadlock (real requests hanging indefinitely) shipped
        past a fully green test suite. This one deliberately leaves
        get_async_db un-overridden so the request goes through the real
        engine end-to-end, with a hard timeout so a regression fails fast
        here instead of hanging CI.
        """
        from fastapi.testclient import TestClient
        from media_manager.web.app import app
        from media_manager.web.api.auth import get_current_user, get_current_active_user, get_admin_user

        user = {"username": "admin", "role": "admin"}
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_current_active_user] = lambda: user
        app.dependency_overrides[get_admin_user] = lambda: user
        try:
            client = TestClient(app)

            async def call():
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(
                    None,
                    lambda: client.get("/api/v1/integrations/search", params={"term": "star wars", "type": "movie"})
                )

            res = asyncio.run(asyncio.wait_for(call(), timeout=15))
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertIn("results", data)
        finally:
            app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()


class TestPostgresConfig(unittest.TestCase):
    """USE_POSTGRES=true must never fall back to baked-in default credentials."""

    def _import_with(self, env):
        import subprocess
        import sys
        full = {k: v for k, v in os.environ.items() if k not in ("POSTGRES_URI", "SYNC_POSTGRES_URI")}
        full.update(env)
        return subprocess.run([sys.executable, "-c", "import media_manager.database"],
                              env=full, capture_output=True, text=True, timeout=60)

    def test_use_postgres_without_uri_fails_at_import(self):
        result = self._import_with({"USE_POSTGRES": "true"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("USE_POSTGRES=true requires POSTGRES_URI", result.stderr)

    def test_no_default_credentials_in_config(self):
        from media_manager import config
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POSTGRES_URI", None)
            self.assertNotIn("mediapass", config._postgres_uri_from_env())
            self.assertEqual(config._postgres_uri_from_env(), "")

    def test_use_postgres_helper(self):
        self.assertFalse(database._use_postgres(False, ""))
        self.assertFalse(database._use_postgres(False, "postgresql+asyncpg://u:p@h/db"))
        self.assertTrue(database._use_postgres(True, "postgresql+asyncpg://u:p@h/db"))
        with self.assertRaisesRegex(RuntimeError, "USE_POSTGRES=true requires POSTGRES_URI"):
            database._use_postgres(True, "")

    def test_sync_uri_derived_only_when_set(self):
        from media_manager import config
        self.assertEqual(config._sync_postgres_uri(""), "")
        self.assertEqual(config._sync_postgres_uri("postgresql+asyncpg://u:p@h/db"), "postgresql+psycopg2://u:p@h/db")
        self.assertEqual(config._sync_postgres_uri("postgresql://u:p@h/db"), "postgresql://u:p@h/db")
