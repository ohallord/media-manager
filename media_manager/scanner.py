import os
import logging
from datetime import datetime
from guessit import guessit
from media_manager.config import MEDIA_DIRECTORIES, VIDEO_EXTENSIONS, AUDIO_EXTENSIONS
from media_manager.metadata import apply_probe_details, get_video_metadata
from media_manager.database import SyncSessionLocal
from media_manager.models import MediaItem, MonitoredDirectory
from media_manager.quality import calculate_quality_score, update_duplicate_groups

logger = logging.getLogger(__name__)

def guess_text(guess, key, fallback=""):
    value = guess.get(key)
    if isinstance(value, list):
        value = " ".join(str(t) for t in value)
    return str(value) if value else fallback

def guess_int(guess, key):
    value = guess.get(key)
    if isinstance(value, list):
        value = value[0] if value else None
    return int(value) if value is not None else None

def delete_media_item_by_path(path: str):
    """
    Removes the PostgreSQL MediaItem row for a path, e.g. after FileBot
    renames a file elsewhere. Without this, the old path's row lingers,
    gets grouped as a "duplicate" of the file's new location, and can be
    the row a tie-break keeps - deleting the real file at the new path
    instead of the stale row.
    """
    session = SyncSessionLocal()
    try:
        session.query(MediaItem).filter_by(path=path).delete()
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Error deleting stale MediaItem for {path}: {e}")
    finally:
        session.close()

def _is_transcode_output_in_flight(file_path: str) -> bool:
    from media_manager.web.services.transcode_jobs import swap_locked_sync
    session = SyncSessionLocal()
    try:
        return file_path in swap_locked_sync(session)["output_paths"]
    except Exception:
        return False
    finally:
        session.close()


def scan_all():
    """
    Scans all media directories and updates the PostgreSQL database.
    Also cleans up database records for deleted files.
    """
    session = SyncSessionLocal()
    try:
        db_dirs = [d.path for d in session.query(MonitoredDirectory).all()]
    except Exception as e:
        logger.warning(f"Failed to query monitored directories from DB: {e}")
        db_dirs = []
    finally:
        session.close()

    scan_dirs = db_dirs if db_dirs else MEDIA_DIRECTORIES

    all_extensions = VIDEO_EXTENSIONS + AUDIO_EXTENSIONS
    for directory in scan_dirs:
        if not os.path.exists(directory):
            logger.warning(f"Directory {directory} does not exist. Skipping.")
            continue
            
        logger.info(f"Scanning directory: {directory}...")
        for root, dirs, files in os.walk(directory):
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in all_extensions:
                    file_path = os.path.join(root, file)
                    process_file(file_path, storage_volume=directory)

    # Re-calculate duplicate groups
    update_duplicate_groups()
    logger.info("Media inventory scan completed.")

def process_file(file_path: str, storage_volume: str = "/media"):
    """
    Processes a single file: extracts metadata via ffprobe, calculates quality score, and upserts to DB.
    Returns True when the row was upserted with a successful ffprobe result.
    """
    try:
        if _is_transcode_output_in_flight(file_path):
            # The transcoder is mid-swap: its completion call updates the
            # original row to this path, so don't index a second one.
            logger.debug(f"Skipping in-flight transcode output: {file_path}")
            return
        metadata = get_video_metadata(file_path)
        if not metadata:
            return
            
        guess = guessit(os.path.basename(file_path))
        
        # Skip sample files
        if guess.get("release_group") == "sample" or "sample" in file_path.lower():
            logger.debug(f"Skipping sample file: {file_path}")
            return

        media_type = guess.get("type", "movie")
        if "tv" in file_path.lower() or "tvshows" in file_path.lower():
            media_type = "tv"
        elif "movie" in file_path.lower():
            media_type = "movie"

        title = guess_text(guess, "title", os.path.splitext(os.path.basename(file_path))[0])
        season = guess_int(guess, "season")
        episode = guess_int(guess, "episode")

        resolution_label = metadata.get("resolution_label") or "SD"
        video_codec = metadata.get("video_codec") or "unknown"
        audio_codec = metadata.get("audio_codec") or "unknown"
        audio_channels = int(metadata.get("audio_channels") or 2)
        bitrate = int(metadata.get("bit_rate") or 0)
        size_bytes = int(metadata.get("size") or os.path.getsize(file_path))
        height = int(metadata.get("height") or 0)
        width = int(metadata.get("width") or 0)

        quality_score = calculate_quality_score(
            resolution=resolution_label,
            height=height,
            audio_codec=audio_codec,
            audio_channels=audio_channels,
            bitrate=bitrate,
            path=file_path,
            video_codec=video_codec,
            video_bitrate=(metadata.get("details") or {}).get("video_bitrate"),
        )

        file_created_at = datetime.fromtimestamp(os.path.getmtime(file_path))

        session = SyncSessionLocal()
        try:
            item = session.query(MediaItem).filter_by(path=file_path).first()
            if not item:
                item = MediaItem(
                    path=file_path,
                    storage_volume=storage_volume,
                    title=title,
                    media_type=media_type,
                    season=season,
                    episode=episode,
                    resolution=resolution_label,
                    height=height,
                    width=width,
                    video_codec=video_codec,
                    audio_codec=audio_codec,
                    audio_channels=audio_channels,
                    bitrate=bitrate,
                    size_bytes=size_bytes,
                    quality_score=quality_score,
                    file_created_at=file_created_at,
                    last_scanned_at=datetime.utcnow()
                )
                apply_probe_details(item, metadata.get("details"))
                session.add(item)
            else:
                item.title = title
                item.media_type = media_type
                item.season = season
                item.episode = episode
                item.resolution = resolution_label
                item.height = height
                item.width = width
                item.video_codec = video_codec
                item.audio_codec = audio_codec
                item.audio_channels = audio_channels
                item.bitrate = bitrate
                item.size_bytes = size_bytes
                item.quality_score = quality_score
                item.last_scanned_at = datetime.utcnow()
                apply_probe_details(item, metadata.get("details"))
            
            session.commit()
            logger.debug(f"[NEW_MEDIA] Indexed: {title} ({resolution_label}) - {file_path}")
            return bool(metadata.get("details"))
        except Exception as e:
            session.rollback()
            logger.error(f"Error upserting DB item {file_path}: {e}")
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error processing file {file_path}: {e}")
