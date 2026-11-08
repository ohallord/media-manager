import logging
import os
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, sessionmaker
from media_manager.config import POSTGRES_URI, SYNC_POSTGRES_URI, DB_PATH

logger = logging.getLogger(__name__)

Base = declarative_base()

# Check if PostgreSQL is explicitly configured or if fallback SQLite should be used
def _use_postgres(requested: bool, uri: str) -> bool:
    if not requested:
        return False
    if not uri:
        raise RuntimeError("USE_POSTGRES=true requires POSTGRES_URI")
    return "postgresql" in uri


USE_POSTGRES = _use_postgres(os.getenv("USE_POSTGRES", "false").lower() in ["true", "1", "yes"], POSTGRES_URI)

if USE_POSTGRES:
    async_engine = create_async_engine(
        POSTGRES_URI,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20
    )
    sync_engine = create_engine(
        SYNC_POSTGRES_URI,
        echo=False,
        pool_pre_ping=True
    )
else:
    # SQLite Fallback for standalone local operation without external PostgreSQL container
    logger.info(f"Using SQLite database engine at {DB_PATH}")

    # On a homelab checkout, this file is written concurrently by the web
    # server AND the separate `media-manager-watcher.service` process (both
    # point at the same on-disk DB_PATH). SQLite's default
    # rollback-journal mode takes an exclusive lock for the duration of a
    # write and gives readers/other writers zero retry window, so a read
    # landing during a watcher write fails immediately with "database is
    # locked".
    #
    # journal_mode is a persistent, file-level setting (stored in the DB
    # file's header, not per-connection) - once WAL is on, it stays on for
    # every future connection from any process, so it only needs setting
    # once here via a plain standalone sqlite3 connection, entirely outside
    # SQLAlchemy's engine machinery. This is deliberate: an earlier attempt
    # set it via a SQLAlchemy `event.listens_for(async_engine.sync_engine,
    # "connect")` sync callback, which deadlocked real requests under actual
    # async server load (passed every unit test - the failure only showed up
    # against the live running server, not a fresh test process) - seting it
    # here avoids the async/greenlet bridge entirely.
    if DB_PATH != ":memory:":
        try:
            import sqlite3
            _wal_conn = sqlite3.connect(DB_PATH, timeout=10)
            try:
                _wal_conn.execute("PRAGMA journal_mode=WAL")
            finally:
                _wal_conn.close()
        except Exception as e:
            logger.warning(f"Could not enable WAL journal mode on {DB_PATH}: {e}")

    # busy_timeout IS per-connection, but this is set via `timeout`, a plain
    # connect-time argument forwarded straight to sqlite3.connect()/
    # aiosqlite.connect() - not a custom callback - so it carries none of the
    # above risk. This is the standard SQLAlchemy-documented way to configure
    # it. Retries internally for up to 30s on any lock window WAL doesn't
    # eliminate (e.g. two near-simultaneous writers) instead of raising
    # immediately.
    async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{DB_PATH}", echo=False, connect_args={"timeout": 30}
    )
    sync_engine = create_engine(
        f"sqlite:///{DB_PATH}", echo=False, connect_args={"timeout": 30}
    )

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False
)

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autocommit=False,
    autoflush=False
)

async def get_async_db():
    """Dependency for FastAPI async routes"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

def get_sync_db():
    """Context manager / provider for synchronous watcher / scanner operations"""
    db = SyncSessionLocal()
    try:
        return db
    finally:
        db.close()

# Columns added to media_items after the table first shipped. create_all()
# never alters existing tables, so both init paths ALTER these in (no-op once
# present). Keep in sync with the MediaItem rich-probe columns.
_MEDIA_ITEMS_COLUMNS = [
    ("video_profile", "VARCHAR(64)"),
    ("pix_fmt", "VARCHAR(32)"),
    ("bit_depth", "INTEGER"),
    ("hdr_format", "VARCHAR(16)"),
    ("dv_profile", "INTEGER"),
    ("color_transfer", "VARCHAR(32)"),
    ("color_primaries", "VARCHAR(32)"),
    ("field_order", "VARCHAR(16)"),
    ("frame_rate", "FLOAT"),
    ("video_bitrate", "BIGINT"),
    ("container", "VARCHAR(64)"),
    ("duration_seconds", "FLOAT"),
    ("audio_stream_count", "INTEGER"),
    ("subtitle_stream_count", "INTEGER"),
    ("attachment_count", "INTEGER"),
    ("probed_at", "DATETIME"),
    ("probe_version", "INTEGER"),
]

# Same idea for other tables that gained columns: (table, column, type).
_EXTRA_COLUMNS = [
    ("users", "token_version", "INTEGER NOT NULL DEFAULT 0"),
    ("service_heartbeats", "version", "VARCHAR(32)"),
    ("service_heartbeats", "detail_json", "TEXT"),
    ("transcode_control", "pause_on", "VARCHAR(16) NOT NULL DEFAULT 'transcode_only'"),
    ("transcode_control", "notify_emails", "TEXT"),
    ("transcode_control", "failure_streak_threshold", "INTEGER NOT NULL DEFAULT 3"),
    ("transcode_control", "last_daily_summary_at", "DATETIME"),
    ("transcode_control", "last_silent_alert_at", "DATETIME"),
    ("transcode_control", "last_streak_alert_job_id", "VARCHAR(36)"),
    ("transcode_control", "last_gpu_alert_at", "DATETIME"),
]

async def init_db_schema():
    """Initializes tables asynchronously"""
    import media_manager.models  # Register all ORM models with Base.metadata
    async with async_engine.begin() as conn:
        try:
            from sqlalchemy import text
            res = await conn.execute(text("PRAGMA table_info(app_log_events)"))
            info = res.fetchall()
            for col in info:
                if col[1] == "id" and "BIGINT" in str(col[2]).upper():
                    await conn.execute(text("DROP TABLE app_log_events"))
                    break
        except Exception:
            pass
        await conn.run_sync(Base.metadata.create_all)
        for col, col_type in [
            ("first_name", "VARCHAR(64)"),
            ("last_name", "VARCHAR(64)"),
            ("phone", "VARCHAR(64)"),
            ("two_factor_method", "VARCHAR(32) DEFAULT 'none'"),
            ("two_factor_enabled", "BOOLEAN DEFAULT 0"),
            ("last_login_at", "DATETIME"),
            ("must_change_password", "BOOLEAN DEFAULT 0")
        ]:
            try:
                from sqlalchemy import text
                await conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {col_type}"))
            except Exception:
                pass
        try:
            from sqlalchemy import text
            await conn.execute(text("ALTER TABLE metadata_cache ADD COLUMN requested_by VARCHAR(255)"))
        except Exception:
            pass
        try:
            from sqlalchemy import text
            await conn.execute(text("ALTER TABLE integration_configs ADD COLUMN extra_json TEXT"))
        except Exception:
            pass
        for col, col_type in _MEDIA_ITEMS_COLUMNS:
            try:
                from sqlalchemy import text
                await conn.execute(text(f"ALTER TABLE media_items ADD COLUMN {col} {col_type}"))
            except Exception:
                pass
        for table, col, col_type in _EXTRA_COLUMNS:
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
            except Exception:
                pass
    logger.info("Database schema initialized successfully.")

def init_db_schema_sync():
    """Initializes tables synchronously for CLI init"""
    import media_manager.models  # Register all ORM models with Base.metadata
    try:
        from sqlalchemy import text
        with sync_engine.connect() as conn:
            res = conn.execute(text("PRAGMA table_info(app_log_events)"))
            info = res.fetchall()
            for col in info:
                if col[1] == "id" and "BIGINT" in str(col[2]).upper():
                    conn.execute(text("DROP TABLE app_log_events"))
                    break
    except Exception:
        pass
    Base.metadata.create_all(bind=sync_engine)
    with sync_engine.connect() as conn:
        from sqlalchemy import text
        for col, col_type in [
            ("first_name", "VARCHAR(64)"),
            ("last_name", "VARCHAR(64)"),
            ("phone", "VARCHAR(64)"),
            ("two_factor_method", "VARCHAR(32) DEFAULT 'none'"),
            ("two_factor_enabled", "BOOLEAN DEFAULT 0"),
            ("last_login_at", "DATETIME"),
            ("must_change_password", "BOOLEAN DEFAULT 0")
        ]:
            try:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {col_type}"))
            except Exception:
                pass
        try:
            conn.execute(text("ALTER TABLE metadata_cache ADD COLUMN requested_by VARCHAR(255)"))
        except Exception:
            pass
        try:
            conn.execute(text("ALTER TABLE integration_configs ADD COLUMN extra_json TEXT"))
        except Exception:
            pass
        for col, col_type in _MEDIA_ITEMS_COLUMNS:
            try:
                conn.execute(text(f"ALTER TABLE media_items ADD COLUMN {col} {col_type}"))
            except Exception:
                pass
        for table, col, col_type in _EXTRA_COLUMNS:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
            except Exception:
                pass
    logger.info("Synchronous database schema initialized.")
