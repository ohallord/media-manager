import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from sqlalchemy import (
    Column, String, Integer, BigInteger, Boolean, Text, DateTime, ForeignKey, Index, Float, text
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from media_manager.database import Base

def generate_uuid_str():
    return str(uuid.uuid4())

class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=generate_uuid_str)
    username = Column(String(64), unique=True, nullable=False, index=True)
    first_name = Column(String(64), nullable=True)
    last_name = Column(String(64), nullable=True)
    email = Column(String(255), nullable=True)
    phone = Column(String(64), nullable=True)
    password_hash = Column(String(255), nullable=True)
    role = Column(String(32), nullable=False, default="user")
    is_active = Column(Boolean, nullable=False, default=True)
    two_factor_method = Column(String(32), nullable=True, default="none")  # 'email', 'phone', or 'none'
    two_factor_enabled = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)
    must_change_password = Column(Boolean, nullable=False, default=False)
    # Bumped on every password change; JWTs carry it as "tv" and older values
    # are rejected, so a password change logs out every other session.
    token_version = Column(Integer, nullable=False, default=0)
    # Email this user when one of their requests becomes available (My Requests toggle).
    notify_on_available = Column(Boolean, nullable=False, default=True)

    passkeys = relationship("PasskeyCredential", back_populates="user", cascade="all, delete-orphan")

class PasskeyCredential(Base):
    __tablename__ = "passkey_credentials"

    id = Column(String(36), primary_key=True, default=generate_uuid_str)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    credential_id = Column(Text, unique=True, nullable=False, index=True)
    public_key = Column(Text, nullable=False)
    sign_count = Column(Integer, nullable=False, default=0)
    transports = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    user = relationship("User", back_populates="passkeys")

class MediaItem(Base):
    __tablename__ = "media_items"

    id = Column(String(36), primary_key=True, default=generate_uuid_str)
    path = Column(Text, unique=True, nullable=False, index=True)
    storage_volume = Column(String(128), nullable=False, index=True)
    title = Column(String(255), nullable=False, index=True)
    media_type = Column(String(32), nullable=False, index=True)  # 'movie' or 'tv'
    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)
    resolution = Column(String(32), nullable=False, index=True)  # '4K', '1080p', etc.
    height = Column(Integer, nullable=False)
    width = Column(Integer, nullable=False)
    video_codec = Column(String(64), nullable=False)
    audio_codec = Column(String(64), nullable=False, index=True)
    audio_channels = Column(Integer, nullable=False)
    bitrate = Column(BigInteger, nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    quality_score = Column(Integer, nullable=False, index=True)
    file_created_at = Column(DateTime, nullable=False)
    last_scanned_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Rich ffprobe details (metadata.extract_details). Nullable: rows indexed
    # before these existed are filled in by the probe backfill.
    video_profile = Column(String(64), nullable=True)
    pix_fmt = Column(String(32), nullable=True)
    bit_depth = Column(Integer, nullable=True)
    hdr_format = Column(String(16), nullable=True)  # 'sdr', 'hdr10', 'hlg', 'dv'
    dv_profile = Column(Integer, nullable=True)
    color_transfer = Column(String(32), nullable=True)
    color_primaries = Column(String(32), nullable=True)
    field_order = Column(String(16), nullable=True)
    frame_rate = Column(Float, nullable=True)
    video_bitrate = Column(BigInteger, nullable=True)
    container = Column(String(64), nullable=True)
    duration_seconds = Column(Float, nullable=True)
    audio_stream_count = Column(Integer, nullable=True)
    subtitle_stream_count = Column(Integer, nullable=True)
    attachment_count = Column(Integer, nullable=True)
    probed_at = Column(DateTime, nullable=True)
    probe_version = Column(Integer, nullable=True)

    duplicate_group_id = Column(String(36), ForeignKey("duplicate_groups.id", ondelete="SET NULL"), nullable=True)

class DuplicateGroup(Base):
    __tablename__ = "duplicate_groups"

    id = Column(String(36), primary_key=True, default=generate_uuid_str)
    group_key = Column(String(255), unique=True, nullable=False, index=True)
    media_title = Column(String(255), nullable=False)
    season_episode = Column(String(64), nullable=True)
    best_media_id = Column(String(36), nullable=True)
    total_count = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

class FileMoveLog(Base):
    __tablename__ = "file_move_logs"

    id = Column(String(36), primary_key=True, default=generate_uuid_str)
    source_path = Column(Text, nullable=False)
    destination_path = Column(Text, nullable=False)
    media_title = Column(String(255), nullable=False)
    quality_before = Column(Integer, nullable=False, default=0)
    quality_after = Column(Integer, nullable=False)
    copy_verified = Column(Boolean, nullable=False, default=False)
    status = Column(String(32), nullable=False)  # 'SUCCESS', 'SKIPPED_QUALITY', 'FAILED'
    error_message = Column(Text, nullable=True)
    moved_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

class AppLogEvent(Base):
    __tablename__ = "app_log_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(String(16), nullable=False, index=True)  # 'INFO', 'WARNING', 'ERROR'
    event_tag = Column(String(64), nullable=False, index=True)  # '[NEW_MEDIA]', '[COPY_VERIFIED]', etc.
    message = Column(Text, nullable=False)
    context_json = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

class IntegrationConfig(Base):
    __tablename__ = "integration_configs"

    id = Column(String(36), primary_key=True, default=generate_uuid_str)
    service_name = Column(String(64), unique=True, nullable=False, index=True)  # 'radarr', 'sonarr', 'sabnzbd', 'fanart'
    api_url = Column(Text, nullable=False)
    api_key_encrypted = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    status = Column(String(32), nullable=False, default="UNKNOWN")  # 'HEALTHY', 'ERROR'
    last_checked_at = Column(DateTime, nullable=True)
    # Generic free-form JSON for service-specific settings that don't warrant their
    # own column/table, e.g. playback source path-remap rules (see PlaybackStat).
    extra_json = Column(Text, nullable=True)

class ArtworkCache(Base):
    __tablename__ = "artwork_cache"

    id = Column(String(36), primary_key=True, default=generate_uuid_str)
    cache_key = Column(String(255), unique=True, nullable=False, index=True)  # e.g. "movie:Inception" or "tv:Stranger Things"
    title = Column(String(255), nullable=False)
    media_type = Column(String(32), nullable=False, default="movie")
    poster_url = Column(Text, nullable=True)
    background_url = Column(Text, nullable=True)
    logo_url = Column(Text, nullable=True)
    banner_url = Column(Text, nullable=True)
    source = Column(String(64), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

class MetadataCache(Base):
    __tablename__ = "metadata_cache"

    id = Column(String(36), primary_key=True, default=generate_uuid_str)
    cache_key = Column(String(255), unique=True, nullable=False, index=True)  # e.g. "movie:inception" or "tv:stranger things"
    title = Column(String(255), nullable=False)
    media_type = Column(String(32), nullable=False, default="movie")
    tmdb_id = Column(Integer, nullable=True)
    overview = Column(Text, nullable=True)
    release_date = Column(String(64), nullable=True)
    rating = Column(Float, nullable=True)  # e.g. 8.4
    vote_count = Column(Integer, nullable=True)
    genres_json = Column(Text, nullable=True)  # JSON string e.g. '["Action", "Sci-Fi"]'
    cast_json = Column(Text, nullable=True)  # JSON string e.g. '["Leonardo DiCaprio", "Joseph Gordon-Levitt"]'
    runtime_minutes = Column(Integer, nullable=True)
    poster_path = Column(Text, nullable=True)
    backdrop_path = Column(Text, nullable=True)
    status = Column(String(64), nullable=True)
    source = Column(String(64), nullable=True, default="tmdb")
    requested_by = Column(String(255), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

class MovieCastCache(Base):
    __tablename__ = "movie_cast_cache"

    id = Column(String(36), primary_key=True, default=generate_uuid_str)
    tmdb_id = Column(Integer, unique=True, nullable=False, index=True)
    cast_json = Column(Text, nullable=True)  # JSON: [{"name":, "character":, "photo_url":}, ...] top 5 billed
    trailer_url = Column(Text, nullable=True)  # YouTube trailer link, or NULL if TMDb has none
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

class MonitoredDirectory(Base):
    __tablename__ = "monitored_directories"

    id = Column(String(36), primary_key=True, default=generate_uuid_str)
    path = Column(Text, unique=True, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class PlaybackStat(Base):
    """
    Per-(MediaItem, source) play stats fetched from Plex/Emby/Jellyfin. Kept as one
    row per source rather than a single pre-merged row so the multi-source merge
    (max play_count / max last_played_at across sources, to avoid double-counting
    the same physical watch tracked by two servers) always runs against current
    per-source truth - see media_manager/web/api/playback.py.
    """
    __tablename__ = "playback_stats"

    id = Column(String(36), primary_key=True, default=generate_uuid_str)
    media_item_id = Column(String(36), ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False, index=True)
    source = Column(String(32), nullable=False, index=True)  # 'plex', 'emby', 'jellyfin'
    source_item_id = Column(String(128), nullable=True)
    play_count = Column(Integer, nullable=False, default=0)
    last_played_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_playback_stats_item_source", "media_item_id", "source", unique=True),
    )


class CleanupSettings(Base):
    """
    Per-media-type configurable thresholds for the Library Cleanup Wizard's
    suggestions (media_manager/web/services/cleanup_suggestions.py) - a row is
    created lazily with these defaults the first time settings are read/written.
    """
    __tablename__ = "cleanup_settings"

    id = Column(String(36), primary_key=True, default=generate_uuid_str)
    media_type = Column(String(32), unique=True, nullable=False)  # 'movie' or 'tv'
    never_played_grace_days = Column(Integer, nullable=False, default=180)
    stale_after_days = Column(Integer, nullable=False, default=365)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ServiceHeartbeat(Base):
    """Last-seen timestamp for host processes that share this DB (e.g. the watcher)."""
    __tablename__ = "service_heartbeats"

    service = Column(String(32), primary_key=True)
    last_beat_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    pid = Column(Integer, nullable=True)
    hostname = Column(String(255), nullable=True)
    version = Column(String(32), nullable=True)
    detail_json = Column(Text, nullable=True)



class RequestNotification(Base):
    """
    One row per (user, requested title) the "your request is ready" sweep has
    handled, so it emails at most once. The row with username "__baseline__"
    marks when the sweep first ran: requests older than that are never emailed.
    """
    __tablename__ = "request_notifications"
    __table_args__ = (Index("ix_request_notifications_user_key", "username", "dedupe_key", unique=True),)

    id = Column(String(36), primary_key=True, default=generate_uuid_str)
    username = Column(String(64), nullable=False)
    dedupe_key = Column(String(255), nullable=False)
    title = Column(String(255), nullable=True)
    emailed = Column(Boolean, nullable=False, default=False)
    notified_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class TranscodeSettings(Base):
    """
    Per-media-type H.265 encode settings (quality, skips, thresholds). Stored
    as JSON merged over transcode.profiles.DEFAULTS, so new settings get their
    default without a schema change. Created lazily on first read.
    """
    __tablename__ = "transcode_settings"

    id = Column(String(36), primary_key=True, default=generate_uuid_str)
    media_type = Column(String(32), unique=True, nullable=False)  # 'movie' or 'tv'
    settings_json = Column(Text, nullable=False, default="{}")
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class TranscodeControl(Base):
    """Single-row global switchboard for the H.265 migration."""
    __tablename__ = "transcode_control"

    id = Column(String(36), primary_key=True, default="global")
    run_state = Column(String(16), nullable=False, default="paused")  # 'running' / 'paused'
    mode = Column(String(16), nullable=False, default="off")  # 'off', 'pilot', 'full'
    source_codecs = Column(Text, nullable=False, default="h264,mpeg4,msmpeg4v3,msmpeg4v2,msmpeg4v1")
    excluded_prefixes_json = Column(Text, nullable=False, default="[]")
    # When to yield the GPU to Plex/Jellyfin/Emby: 'transcode_only' (default),
    # 'any_stream' or 'never'. See web/services/stream_activity.py.
    pause_on = Column(String(16), nullable=False, default="transcode_only")
    # Notifications (web/services/transcode_notify.py). Empty notify_emails = all admins' emails.
    notify_emails = Column(Text, nullable=True)
    failure_streak_threshold = Column(Integer, nullable=False, default=3)
    last_daily_summary_at = Column(DateTime, nullable=True)
    last_silent_alert_at = Column(DateTime, nullable=True)
    last_streak_alert_job_id = Column(String(36), nullable=True)
    last_gpu_alert_at = Column(DateTime, nullable=True)  # set while a GPU-unavailable outage is open
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


# Job states that hold a file: at most one per media item (partial unique index).
TRANSCODE_ACTIVE_STATES = ("pending", "claimed", "running", "needs_recovery")
# Phases where the library file itself is being swapped: scans and the
# stale-row purge must leave the item alone.
TRANSCODE_SWAP_PHASES = ("installing", "swapping", "swapped")


class TranscodeJob(Base):
    """
    One attempt history + write-ahead journal per file being converted.
    state:  pending -> claimed -> running -> done | skipped | failed | cancelled,
            or needs_recovery if the worker vanished mid-swap.
    phase:  where the worker is (queued, preparing, encoding, verifying,
            installing, swapping, swapped, done) - written *before* each
            irreversible step so a restart can finish or roll back.
    """
    __tablename__ = "transcode_jobs"
    __table_args__ = (
        Index(
            "uq_transcode_jobs_active_item", "media_item_id", unique=True,
            sqlite_where=text("state IN ('pending','claimed','running','needs_recovery')"),
            postgresql_where=text("state IN ('pending','claimed','running','needs_recovery')"),
        ),
    )

    id = Column(String(36), primary_key=True, default=generate_uuid_str)
    media_item_id = Column(String(36), ForeignKey("media_items.id", ondelete="SET NULL"), nullable=True, index=True)
    state = Column(String(16), nullable=False, default="pending", index=True)
    phase = Column(String(16), nullable=False, default="queued")
    is_pilot = Column(Boolean, nullable=False, default=False)
    attempts = Column(Integer, nullable=False, default=0)

    lease_owner = Column(String(128), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    progress_pct = Column(Float, nullable=True)
    fps = Column(Float, nullable=True)
    eta_seconds = Column(Integer, nullable=True)

    # Snapshot at enqueue time: the worker refuses to touch a file that changed.
    source_path = Column(Text, nullable=False)
    source_size = Column(BigInteger, nullable=False)
    source_mtime = Column(Float, nullable=True)
    source_codec = Column(String(64), nullable=True)
    title = Column(String(255), nullable=True)
    media_type = Column(String(32), nullable=True)
    est_savings_bytes = Column(BigInteger, nullable=False, default=0)
    plan_json = Column(Text, nullable=True)

    temp_path = Column(Text, nullable=True)
    backup_path = Column(Text, nullable=True)
    output_path = Column(Text, nullable=True)
    output_size = Column(BigInteger, nullable=True)
    output_hash = Column(String(128), nullable=True)
    verify_json = Column(Text, nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    backup_purged_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
