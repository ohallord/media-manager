import os
import shutil
import logging
from guessit import guessit
from media_manager.config import UID, GID, PERMISSIONS
from media_manager.metadata import get_video_metadata
from media_manager.quality import calculate_quality_score
from media_manager.scanner import guess_int, guess_text, process_file
from media_manager.database import SyncSessionLocal
from media_manager.models import MediaItem, FileMoveLog

logger = logging.getLogger(__name__)

def move_if_better(src_path: str, dest_dir: str, media_type: str = None) -> bool:
    """
    Checks if a new file is better than existing versions, copies it, verifies file size,
    records an audit log in PostgreSQL file_move_logs, and truncates the source file.
    """
    session = SyncSessionLocal()
    try:
        if not os.path.exists(src_path):
            logger.error(f"Source file {src_path} does not exist.")
            return False

        new_metadata = get_video_metadata(src_path)
        if not new_metadata or new_metadata.get("resolution_label") == "Unknown":
            logger.error(f"Could not get valid metadata (ffprobe failed) for {src_path}.")
            return False

        guess = guessit(os.path.basename(src_path))
        title = guess_text(guess, "title", os.path.splitext(os.path.basename(src_path))[0])
        season = guess_int(guess, "season")
        episode = guess_int(guess, "episode")
        
        m_type = media_type or guess.get("type", "movie")
        if "tv" in src_path.lower() or "tvshows" in src_path.lower():
            m_type = "tv"

        new_score = calculate_quality_score(
            resolution=new_metadata.get("resolution_label") or "SD",
            height=new_metadata.get("height") or 0,
            audio_codec=new_metadata.get("audio_codec") or "unknown",
            audio_channels=new_metadata.get("audio_channels") or 2,
            bitrate=new_metadata.get("bit_rate") or 0,
            video_codec=new_metadata.get("video_codec"),
            video_bitrate=(new_metadata.get("details") or {}).get("video_bitrate"),
        )

        # Query existing item in DB
        query = session.query(MediaItem).filter(MediaItem.title.ilike(title), MediaItem.media_type == m_type)
        if m_type == "tv" and season is not None and episode is not None:
            query = query.filter(MediaItem.season == season, MediaItem.episode == episode)
        
        best_existing = query.order_by(MediaItem.quality_score.desc()).first()

        existing_score = best_existing.quality_score if best_existing else 0
        should_move = False

        if not best_existing:
            logger.info(f"[NEW_MEDIA] New media detected: '{title}'. Preparing move to {dest_dir}")
            should_move = True
        elif new_score > existing_score:
            logger.info(f"[HIGHER_QUALITY] Higher quality found for '{title}': {new_score} > {existing_score}. Replacing existing copy.")
            should_move = True
        else:
            logger.info(f"[SKIPPED_QUALITY] Existing version of '{title}' is equal or better ({existing_score} >= {new_score}). Skipping move.")
            audit_entry = FileMoveLog(
                source_path=src_path,
                destination_path=os.path.join(dest_dir, os.path.basename(src_path)),
                media_title=title,
                quality_before=existing_score,
                quality_after=new_score,
                copy_verified=False,
                status="SKIPPED_QUALITY"
            )
            session.add(audit_entry)
            session.commit()
            return False

        if should_move:
            # Ensure target parent directories exist
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir, exist_ok=True)
            
            # Apply ownership and 775 permissions to destination directory
            try:
                os.chown(dest_dir, UID, GID)
                os.chmod(dest_dir, 0o775)
            except Exception as e:
                logger.warning(f"Could not update directory permissions for {dest_dir}: {e}")

            dest_path = os.path.join(dest_dir, os.path.basename(src_path))
            src_size = os.path.getsize(src_path)

            logger.info(f"[COPY_START] Copying {src_path} -> {dest_path}")
            shutil.copy2(src_path, dest_path)

            try:
                os.chown(dest_path, UID, GID)
                os.chmod(dest_path, PERMISSIONS)
            except Exception as e:
                logger.warning(f"Could not update permissions for {dest_path}: {e}")

            # Verify copy exact size match
            if not os.path.exists(dest_path) or os.path.getsize(dest_path) != src_size or src_size == 0:
                logger.error(f"[COPY_FAILED] Copy verification failed for {dest_path}. Source file will NOT be truncated.")
                audit_entry = FileMoveLog(
                    source_path=src_path,
                    destination_path=dest_path,
                    media_title=title,
                    quality_before=existing_score,
                    quality_after=new_score,
                    copy_verified=False,
                    status="FAILED",
                    error_message="Destination size mismatch or missing"
                )
                session.add(audit_entry)
                session.commit()
                return False

            logger.info(f"[COPY_VERIFIED] Verified copy at {dest_path} ({src_size} bytes)")

            # Truncate source file safely
            with open(src_path, 'w') as f:
                pass
            logger.info(f"[SOURCE_TRUNCATED] Safely truncated source: {src_path}")

            # Add successful audit log
            audit_entry = FileMoveLog(
                source_path=src_path,
                destination_path=dest_path,
                media_title=title,
                quality_before=existing_score,
                quality_after=new_score,
                copy_verified=True,
                status="SUCCESS"
            )
            session.add(audit_entry)
            session.commit()

            # Index new file in DB
            process_file(dest_path)
            return True

    except Exception as e:
        session.rollback()
        logger.error(f"Error in move_if_better for {src_path}: {e}")
        return False
    finally:
        session.close()
