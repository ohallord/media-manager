import time
import logging
import os
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from .config import SOURCE_DIRECTORIES, VIDEO_EXTENSIONS
from .mover import move_if_better
from .filebot import file_sonarr_episode, run_filebot_artwork
from .metadata import get_video_metadata
from .scanner import process_file, delete_media_item_by_path
from .heartbeat import tick_watcher_heartbeat

# Track files currently being processed to avoid duplicate threads processing the same file
processing_files = set()
processing_lock = threading.Lock()

class MediaHandler(FileSystemEventHandler):
    def __init__(self, to_dir, media_type):
        self.to_dir = to_dir
        self.media_type = media_type

    def on_closed(self, event):
        """
        Triggered when a file is closed (finished being written/copied).
        """
        if event.is_directory:
            return
        file_path = event.src_path
        ext = os.path.splitext(file_path)[1].lower()
        if ext in VIDEO_EXTENSIONS:
            logging.info(f"[EVENT_CLOSED] File closed event detected: {file_path}")
            threading.Thread(target=self.process_event_path, args=(file_path,), daemon=True).start()

    def on_moved(self, event):
        """
        Triggered when a file is renamed (e.g. Syncthing renaming .tmp to final name).
        """
        if event.is_directory:
            return
        file_path = event.dest_path
        ext = os.path.splitext(file_path)[1].lower()
        if ext in VIDEO_EXTENSIONS:
            logging.info(f"[EVENT_MOVED] File moved/renamed event detected: {file_path}")
            threading.Thread(target=self.process_event_path, args=(file_path,), daemon=True).start()

    def on_created(self, event):
        """
        Triggered when a file is created.
        """
        if event.is_directory:
            return
        file_path = event.src_path
        ext = os.path.splitext(file_path)[1].lower()
        if ext in VIDEO_EXTENSIONS:
            logging.info(f"[EVENT_CREATED] File created event detected: {file_path}")
            threading.Thread(target=self.process_event_path, args=(file_path,), daemon=True).start()

    def process_event_path(self, file_path):
        """
        Processes a file path safely: checks for existence, size stability,
        valid metadata (no ffprobe failure), and moves it.
        """
        # Ensure only one thread processes this file path at a time
        with processing_lock:
            if file_path in processing_files:
                logging.debug(f"File {file_path} is already being processed. Skipping duplicate thread.")
                return
            processing_files.add(file_path)

        try:
            if not os.path.exists(file_path):
                return

            # 1. Size check: ignore 0-byte files immediately
            initial_size = os.path.getsize(file_path)
            if initial_size == 0:
                logging.info(f"[SKIPPED_ZERO_BYTE] File {file_path} is 0 bytes. Skipping for now (likely incomplete).")
                return

            # 2. Wait for stability (5 seconds) to ensure copy/download has finished
            time.sleep(5)
            if not os.path.exists(file_path):
                return
            current_size = os.path.getsize(file_path)
            if current_size != initial_size:
                logging.info(f"[SIZE_CHANGING] File {file_path} size is still changing ({initial_size} -> {current_size}). Skipping for now.")
                return

            # 3. Check metadata integrity (ffprobe check)
            metadata = get_video_metadata(file_path)
            if not metadata or metadata.get("resolution_label") == "Unknown":
                logging.info(f"[FFPROBE_FAILED] File {file_path} failed ffprobe check (incomplete or corrupted). Skipping.")
                return

            logging.info(f"[STABLE] File {file_path} is ready and stable (size: {current_size / (1024**2):.2f} MB). Proceeding...")

            if move_if_better(file_path, self.to_dir, self.media_type):
                new_path = os.path.join(self.to_dir, os.path.basename(file_path))
                renamed_paths = run_filebot_artwork(new_path)
                if not renamed_paths:
                    renamed_paths = file_sonarr_episode(new_path)
                
                if renamed_paths:
                    for p in renamed_paths:
                        if os.path.isfile(p):
                            process_file(p)
                    if new_path not in renamed_paths and not os.path.exists(new_path):
                        delete_media_item_by_path(new_path)

        except Exception as e:
            logging.error(f"Error in process_event_path for {file_path}: {e}")
        finally:
            with processing_lock:
                processing_files.discard(file_path)

def daily_duplicate_checker():
    """
    Background worker thread that runs duplicate check once per day (every 24 hours).
    """
    from .quality import update_duplicate_groups
    while True:
        try:
            logging.info("[DAILY_CRON] Running daily automated duplicate check...")
            update_duplicate_groups()
            try:
                from media_manager.web.api.inventory import clear_duplicates_cache
                clear_duplicates_cache()
            except Exception:
                pass
            logging.info("[DAILY_CRON] Daily duplicate check completed successfully.")
        except Exception as e:
            logging.error(f"[DAILY_CRON] Daily duplicate check failed: {e}")
        time.sleep(86400) # Wait 24 Hours

def start_watching():
    """
    Starts watching all source directories and schedules daily duplicate checks.
    """
    threading.Thread(target=daily_duplicate_checker, daemon=True).start()
    
    observer = Observer()
    for source in SOURCE_DIRECTORIES:
        from_dir = source["from_dir"]
        to_dir = source["to_dir"]
        m_type = source.get("type")
        
        if not os.path.exists(from_dir):
            logging.warning(f"Source directory {from_dir} does not exist. Skipping watch.")
            continue
            
        logging.info(f"Setting up watch on {from_dir} -> {to_dir} ({m_type})")
        handler = MediaHandler(to_dir, m_type)
        observer.schedule(handler, from_dir, recursive=True)
        
    observer.start()
    logging.info("Observer started. Monitoring source directories & scheduled daily duplicate checks...")
    tick_watcher_heartbeat()
    try:
        beats = 0
        while True:
            time.sleep(1)
            beats += 1
            if beats % 30 == 0:
                tick_watcher_heartbeat()
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
