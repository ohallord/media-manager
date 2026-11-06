import os
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock

from media_manager import watcher


class FakeEvent:
    def __init__(self, src_path=None, dest_path=None, is_directory=False):
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


class TestMediaHandlerEvents(unittest.TestCase):
    """on_closed/on_moved/on_created just filter by extension and spawn a
    thread - patch threading.Thread so no real thread (and no real 5s sleep
    inside process_event_path) ever starts."""

    def setUp(self):
        self.handler = watcher.MediaHandler(to_dir="/tmp/_watcher_test_dest_", media_type="movie")

    @patch("media_manager.watcher.threading.Thread")
    def test_on_closed_directory_ignored(self, mock_thread):
        self.handler.on_closed(FakeEvent(src_path="/tmp/dir", is_directory=True))
        mock_thread.assert_not_called()

    @patch("media_manager.watcher.threading.Thread")
    def test_on_closed_non_video_ext_ignored(self, mock_thread):
        self.handler.on_closed(FakeEvent(src_path="/tmp/file.txt"))
        mock_thread.assert_not_called()

    @patch("media_manager.watcher.threading.Thread")
    def test_on_closed_video_ext_spawns_thread(self, mock_thread):
        self.handler.on_closed(FakeEvent(src_path="/tmp/file.mkv"))
        mock_thread.assert_called_once()
        _, kwargs = mock_thread.call_args
        self.assertEqual(kwargs["args"], ("/tmp/file.mkv",))

    @patch("media_manager.watcher.threading.Thread")
    def test_on_moved_video_ext_spawns_thread(self, mock_thread):
        self.handler.on_moved(FakeEvent(dest_path="/tmp/renamed.mp4"))
        mock_thread.assert_called_once()

    @patch("media_manager.watcher.threading.Thread")
    def test_on_moved_directory_ignored(self, mock_thread):
        self.handler.on_moved(FakeEvent(dest_path="/tmp/dir", is_directory=True))
        mock_thread.assert_not_called()

    @patch("media_manager.watcher.threading.Thread")
    def test_on_created_video_ext_spawns_thread(self, mock_thread):
        self.handler.on_created(FakeEvent(src_path="/tmp/new.avi"))
        mock_thread.assert_called_once()

    @patch("media_manager.watcher.threading.Thread")
    def test_on_created_directory_ignored(self, mock_thread):
        self.handler.on_created(FakeEvent(src_path="/tmp/dir", is_directory=True))
        mock_thread.assert_not_called()


class TestProcessEventPath(unittest.TestCase):
    def setUp(self):
        self.handler = watcher.MediaHandler(to_dir="/tmp/_watcher_test_dest_", media_type="movie")
        self.tmpdir = tempfile.TemporaryDirectory()
        watcher.processing_files.clear()

    def tearDown(self):
        self.tmpdir.cleanup()
        watcher.processing_files.clear()

    def test_missing_file_returns_early(self):
        self.handler.process_event_path("/tmp/_watcher_test_/does_not_exist.mkv")
        self.assertNotIn("/tmp/_watcher_test_/does_not_exist.mkv", watcher.processing_files)

    def test_zero_byte_file_skipped(self):
        path = os.path.join(self.tmpdir.name, "empty.mkv")
        open(path, "wb").close()
        self.handler.process_event_path(path)
        self.assertNotIn(path, watcher.processing_files)

    @patch("media_manager.watcher.time.sleep")
    def test_size_still_changing_skipped(self, mock_sleep):
        path = os.path.join(self.tmpdir.name, "growing.mkv")
        with open(path, "wb") as f:
            f.write(b"0" * 100)

        original_getsize = os.path.getsize
        call_count = {"n": 0}

        def fake_getsize(p):
            call_count["n"] += 1
            return 100 if call_count["n"] == 1 else 200

        with patch("media_manager.watcher.os.path.getsize", side_effect=fake_getsize):
            self.handler.process_event_path(path)
        mock_sleep.assert_called_once_with(5)

    @patch("media_manager.watcher.time.sleep")
    @patch("media_manager.watcher.get_video_metadata", return_value={"resolution_label": "Unknown"})
    def test_ffprobe_failure_skipped(self, mock_meta, mock_sleep):
        path = os.path.join(self.tmpdir.name, "bad.mkv")
        with open(path, "wb") as f:
            f.write(b"0" * 100)
        self.handler.process_event_path(path)
        mock_meta.assert_called_once()

    @patch("media_manager.watcher.time.sleep")
    @patch("media_manager.watcher.get_video_metadata", return_value={"resolution_label": "1080p"})
    @patch("media_manager.watcher.move_if_better", return_value=False)
    def test_move_not_better_stops_before_filebot(self, mock_move, mock_meta, mock_sleep):
        path = os.path.join(self.tmpdir.name, "good.mkv")
        with open(path, "wb") as f:
            f.write(b"0" * 100)
        with patch("media_manager.watcher.run_filebot_artwork") as mock_filebot:
            self.handler.process_event_path(path)
            mock_filebot.assert_not_called()

    @patch("media_manager.watcher.process_file")
    @patch("media_manager.watcher.run_filebot_artwork")
    @patch("media_manager.watcher.time.sleep")
    @patch("media_manager.watcher.get_video_metadata", return_value={"resolution_label": "1080p"})
    @patch("media_manager.watcher.move_if_better", return_value=True)
    def test_successful_move_runs_filebot_and_indexes_renamed_files(
        self, mock_move, mock_meta, mock_sleep, mock_filebot, mock_process_file
    ):
        path = os.path.join(self.tmpdir.name, "good.mkv")
        with open(path, "wb") as f:
            f.write(b"0" * 100)

        renamed_path = os.path.join(self.tmpdir.name, "renamed.mkv")
        with open(renamed_path, "wb") as f:
            f.write(b"0")
        mock_filebot.return_value = [renamed_path]

        self.handler.process_event_path(path)
        mock_process_file.assert_called_once_with(renamed_path)

    @patch("media_manager.watcher.process_file")
    @patch("media_manager.watcher.file_sonarr_episode")
    @patch("media_manager.watcher.run_filebot_artwork", return_value=[])
    @patch("media_manager.watcher.time.sleep")
    @patch("media_manager.watcher.get_video_metadata", return_value={"resolution_label": "1080p"})
    @patch("media_manager.watcher.move_if_better", return_value=True)
    def test_filebot_miss_falls_back_to_sonarr_name(
        self, mock_move, mock_meta, mock_sleep, mock_filebot, mock_fallback, mock_process_file
    ):
        # FileBot left "Doc (US) - 3x02 - ..." loose in the library root,
        # where Plex never lists it.
        path = os.path.join(self.tmpdir.name, "Doc (US) - 3x02 - Ready or Not HDTV-720p.mkv")
        with open(path, "wb") as f:
            f.write(b"0" * 100)
        filed = os.path.join(self.tmpdir.name, "filed.mkv")
        with open(filed, "wb") as f:
            f.write(b"0")
        mock_fallback.return_value = [filed]

        self.handler.process_event_path(path)
        mock_fallback.assert_called_once()
        mock_process_file.assert_called_once_with(filed)

    @patch("media_manager.watcher.time.sleep")
    @patch("media_manager.watcher.get_video_metadata", return_value={"resolution_label": "1080p"})
    @patch("media_manager.watcher.move_if_better", return_value=True)
    @patch("media_manager.watcher.delete_media_item_by_path")
    def test_new_path_missing_after_move_deletes_media_item(
        self, mock_delete, mock_move, mock_meta, mock_sleep
    ):
        # delete_media_item_by_path only fires when filebot DID rename the
        # file elsewhere (renamed_paths non-empty) and the pre-rename
        # new_path no longer exists on disk - both must hold.
        path = os.path.join(self.tmpdir.name, "good.mkv")
        with open(path, "wb") as f:
            f.write(b"0" * 100)

        renamed_path = os.path.join(self.tmpdir.name, "elsewhere.mkv")
        with open(renamed_path, "wb") as f:
            f.write(b"0")

        with patch("media_manager.watcher.run_filebot_artwork", return_value=[renamed_path]):
            self.handler.process_event_path(path)
        mock_delete.assert_called_once()

    def test_duplicate_processing_is_skipped(self):
        path = os.path.join(self.tmpdir.name, "dup.mkv")
        with open(path, "wb") as f:
            f.write(b"0" * 100)
        watcher.processing_files.add(path)
        try:
            # Should return immediately without raising, since it's already "in progress".
            self.handler.process_event_path(path)
        finally:
            watcher.processing_files.discard(path)

    @patch("media_manager.watcher.get_video_metadata", side_effect=RuntimeError("boom"))
    @patch("media_manager.watcher.time.sleep")
    def test_exception_is_caught_and_processing_flag_cleared(self, mock_sleep, mock_meta):
        path = os.path.join(self.tmpdir.name, "explode.mkv")
        with open(path, "wb") as f:
            f.write(b"0" * 100)
        self.handler.process_event_path(path)
        self.assertNotIn(path, watcher.processing_files)


class TestDailyDuplicateChecker(unittest.TestCase):
    @patch("media_manager.watcher.time.sleep", side_effect=StopIteration)
    @patch("media_manager.quality.update_duplicate_groups")
    def test_runs_one_cycle_then_stops(self, mock_update, mock_sleep):
        with self.assertRaises(StopIteration):
            watcher.daily_duplicate_checker()
        mock_update.assert_called_once()

    @patch("media_manager.watcher.time.sleep", side_effect=StopIteration)
    @patch("media_manager.quality.update_duplicate_groups", side_effect=RuntimeError("db down"))
    def test_exception_in_cycle_is_caught_and_loop_continues(self, mock_update, mock_sleep):
        with self.assertRaises(StopIteration):
            watcher.daily_duplicate_checker()
        mock_update.assert_called_once()


class TestStartWatching(unittest.TestCase):
    @patch("media_manager.watcher.time.sleep", side_effect=KeyboardInterrupt)
    @patch("media_manager.watcher.threading.Thread")
    @patch("media_manager.watcher.Observer")
    @patch("media_manager.watcher.SOURCE_DIRECTORIES", [
        {"from_dir": "/tmp/_watcher_test_exists_", "to_dir": "/tmp/_watcher_test_dest_", "type": "movie"},
        {"from_dir": "/tmp/_watcher_test_missing_", "to_dir": "/tmp/_watcher_test_dest2_", "type": "tv"},
    ])
    def test_schedules_existing_dirs_and_skips_missing(self, mock_observer_cls, mock_thread, mock_sleep):
        mock_observer = MagicMock()
        mock_observer_cls.return_value = mock_observer

        with patch("media_manager.watcher.os.path.exists", side_effect=lambda p: p == "/tmp/_watcher_test_exists_"):
            watcher.start_watching()

        mock_observer.schedule.assert_called_once()
        args, kwargs = mock_observer.schedule.call_args
        self.assertEqual(args[1], "/tmp/_watcher_test_exists_")
        mock_observer.start.assert_called_once()
        mock_observer.stop.assert_called_once()
        mock_observer.join.assert_called_once()


if __name__ == "__main__":
    unittest.main()
