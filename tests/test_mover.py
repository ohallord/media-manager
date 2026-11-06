import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from media_manager.mover import move_if_better
from media_manager.database import SyncSessionLocal
from media_manager.models import MediaItem, FileMoveLog

GOOD_METADATA = {
    "resolution_label": "1080p", "height": 1080, "width": 1920,
    "video_codec": "h264", "audio_codec": "AC3", "audio_channels": 6,
    "bit_rate": 5_000_000,
}


class TestMoveIfBetter(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dest_dir = os.path.join(self.tmpdir.name, "dest")
        self.seeded_ids = []

    def tearDown(self):
        self.tmpdir.cleanup()
        if self.seeded_ids:
            session = SyncSessionLocal()
            try:
                session.query(MediaItem).filter(MediaItem.id.in_(self.seeded_ids)).delete(synchronize_session=False)
                session.commit()
            finally:
                session.close()
        session = SyncSessionLocal()
        try:
            session.query(FileMoveLog).filter(FileMoveLog.media_title.like("MoverTest%")).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()

    def _make_src(self, name="MoverTestMovie (2020).mkv", size=1000):
        src = os.path.join(self.tmpdir.name, name)
        with open(src, "wb") as f:
            f.write(b"0" * size)
        return src

    def _seed_existing(self, title, score, media_type="movie", season=None, episode=None):
        session = SyncSessionLocal()
        try:
            item = MediaItem(
                path=f"/tmp/_mover_seed_/{title}.mkv", storage_volume="/tmp",
                title=title, media_type=media_type, season=season, episode=episode,
                resolution="720p", height=720, width=1280,
                video_codec="h264", audio_codec="AAC", audio_channels=2,
                bitrate=1_000_000, size_bytes=1, quality_score=score,
                file_created_at=datetime.utcnow(),
            )
            session.add(item)
            session.commit()
            self.seeded_ids.append(item.id)
        finally:
            session.close()

    def test_source_missing_returns_false(self):
        result = move_if_better("/tmp/_mover_test_/does_not_exist.mkv", self.dest_dir)
        self.assertFalse(result)

    @patch("media_manager.mover.get_video_metadata", return_value={"resolution_label": "Unknown"})
    def test_bad_metadata_returns_false(self, mock_meta):
        src = self._make_src()
        result = move_if_better(src, self.dest_dir)
        self.assertFalse(result)

    @patch("media_manager.mover.process_file")
    @patch("media_manager.mover.get_video_metadata", return_value=GOOD_METADATA)
    def test_new_media_moves_successfully(self, mock_meta, mock_process_file):
        src = self._make_src()
        result = move_if_better(src, self.dest_dir, media_type="movie")
        self.assertTrue(result)
        dest_path = os.path.join(self.dest_dir, os.path.basename(src))
        self.assertTrue(os.path.exists(dest_path))
        # Source is truncated (copy-then-truncate), not removed.
        self.assertTrue(os.path.exists(src))
        self.assertEqual(os.path.getsize(src), 0)
        mock_process_file.assert_called_once_with(dest_path)

    @patch("media_manager.mover.process_file")
    @patch("media_manager.mover.get_video_metadata", return_value=GOOD_METADATA)
    def test_skips_when_existing_is_better(self, mock_meta, mock_process_file):
        self._seed_existing("MoverTestMovie", score=999999)
        src = self._make_src()
        result = move_if_better(src, self.dest_dir, media_type="movie")
        self.assertFalse(result)
        mock_process_file.assert_not_called()

        session = SyncSessionLocal()
        try:
            log = session.query(FileMoveLog).filter_by(source_path=src).first()
            self.assertIsNotNone(log)
            self.assertEqual(log.status, "SKIPPED_QUALITY")
        finally:
            session.close()

    @patch("media_manager.mover.process_file")
    @patch("media_manager.mover.get_video_metadata", return_value=GOOD_METADATA)
    def test_replaces_when_new_is_better(self, mock_meta, mock_process_file):
        self._seed_existing("MoverTestMovie", score=1)
        src = self._make_src()
        result = move_if_better(src, self.dest_dir, media_type="movie")
        self.assertTrue(result)
        mock_process_file.assert_called_once()

    @patch("media_manager.mover.process_file")
    @patch("media_manager.mover.shutil.copy2")
    @patch("media_manager.mover.get_video_metadata", return_value=GOOD_METADATA)
    def test_copy_verification_failure_logged(self, mock_meta, mock_copy2, mock_process_file):
        src = self._make_src()
        mock_copy2.return_value = None  # doesn't actually create the dest file
        result = move_if_better(src, self.dest_dir, media_type="movie")
        self.assertFalse(result)
        mock_process_file.assert_not_called()

        session = SyncSessionLocal()
        try:
            log = session.query(FileMoveLog).filter_by(source_path=src).first()
            self.assertIsNotNone(log)
            self.assertEqual(log.status, "FAILED")
        finally:
            session.close()

    @patch("media_manager.mover.get_video_metadata", side_effect=RuntimeError("boom"))
    def test_unexpected_exception_is_caught_and_rolled_back(self, mock_meta):
        src = self._make_src()
        result = move_if_better(src, self.dest_dir, media_type="movie")
        self.assertFalse(result)

    @patch("media_manager.mover.process_file")
    @patch("media_manager.mover.get_video_metadata", return_value=GOOD_METADATA)
    def test_tv_type_inferred_from_path(self, mock_meta, mock_process_file):
        src = os.path.join(self.tmpdir.name, "MoverTestShow.S01E01.mkv")
        with open(src, "wb") as f:
            f.write(b"0" * 1000)
        result = move_if_better(src, os.path.join(self.tmpdir.name, "tvshows_dest"), media_type=None)
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
