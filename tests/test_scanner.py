import os
import tempfile
import shutil
import unittest
from datetime import datetime
from unittest.mock import patch

from media_manager.database import SyncSessionLocal
from media_manager.models import MediaItem, MonitoredDirectory
from media_manager.metadata import PROBE_VERSION
from media_manager.scanner import delete_media_item_by_path, guess_int, guess_text, process_file, scan_all

FAKE_METADATA = {
    "path": "unused", "format_name": "matroska", "duration": 100.0, "size": 12345,
    "bit_rate": 5_000_000, "video_codec": "h264", "width": 1920, "height": 1080,
    "audio_codec": "aac", "audio_channels": 2, "resolution_label": "1080p",
}


class TestGuessFields(unittest.TestCase):
    def test_text_joins_lists_and_int_takes_first(self):
        guess = {"title": ["The", "Matrix"], "season": [2, 3], "episode": None}
        self.assertEqual(guess_text(guess, "title", "fallback"), "The Matrix")
        self.assertEqual(guess_int(guess, "season"), 2)
        self.assertIsNone(guess_int(guess, "episode"))
        self.assertEqual(guess_text({}, "title", "fallback"), "fallback")


class TestDeleteMediaItemByPath(unittest.TestCase):
    def test_deletes_existing_row(self):
        path = "/tmp/_ut_scanner_delete_test.mkv"
        session = SyncSessionLocal()
        try:
            session.add(MediaItem(
                path=path, storage_volume="/tmp", title="X", media_type="movie",
                resolution="1080p", height=1080, width=1920, video_codec="h264",
                audio_codec="aac", audio_channels=2, bitrate=1000, size_bytes=1000,
                quality_score=100, file_created_at=datetime.utcnow(),
            ))
            session.commit()
        finally:
            session.close()

        delete_media_item_by_path(path)

        session = SyncSessionLocal()
        try:
            self.assertIsNone(session.query(MediaItem).filter_by(path=path).first())
        finally:
            session.close()

    def test_deleting_nonexistent_path_does_not_raise(self):
        delete_media_item_by_path("/tmp/_ut_scanner_never_existed.mkv")


class TestProcessFile(unittest.TestCase):
    def setUp(self):
        self.scan_dir = tempfile.mkdtemp(prefix="mm_proc_file_")
        self.path = os.path.join(self.scan_dir, "Example.Movie.2010.1080p.WEB-DL.mkv")
        with open(self.path, "wb") as fh:
            fh.write(b"data")

    def tearDown(self):
        shutil.rmtree(self.scan_dir, ignore_errors=True)
        session = SyncSessionLocal()
        try:
            session.query(MediaItem).filter_by(path=self.path).delete()
            session.commit()
        finally:
            session.close()

    def test_inserts_new_media_item(self):
        with patch("media_manager.scanner.get_video_metadata", return_value=FAKE_METADATA):
            process_file(self.path, storage_volume="/tmp")

        session = SyncSessionLocal()
        try:
            item = session.query(MediaItem).filter_by(path=self.path).first()
            self.assertIsNotNone(item)
            self.assertEqual(item.resolution, "1080p")
            self.assertGreater(item.quality_score, 0)
        finally:
            session.close()

    def test_updates_existing_media_item(self):
        with patch("media_manager.scanner.get_video_metadata", return_value=FAKE_METADATA):
            process_file(self.path, storage_volume="/tmp")
            updated_meta = dict(FAKE_METADATA, resolution_label="4K", height=2160)
            with patch("media_manager.scanner.get_video_metadata", return_value=updated_meta):
                process_file(self.path, storage_volume="/tmp")

        session = SyncSessionLocal()
        try:
            items = session.query(MediaItem).filter_by(path=self.path).all()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].resolution, "4K")
        finally:
            session.close()

    def test_returns_early_when_no_metadata(self):
        with patch("media_manager.scanner.get_video_metadata", return_value=None):
            process_file(self.path, storage_volume="/tmp")

        session = SyncSessionLocal()
        try:
            self.assertIsNone(session.query(MediaItem).filter_by(path=self.path).first())
        finally:
            session.close()

    def test_skips_sample_file_by_path(self):
        sample_path = self.path.replace(".mkv", ".sample.mkv")
        os.rename(self.path, sample_path)
        self.path = sample_path
        with patch("media_manager.scanner.get_video_metadata", return_value=FAKE_METADATA):
            process_file(self.path, storage_volume="/tmp")

        session = SyncSessionLocal()
        try:
            self.assertIsNone(session.query(MediaItem).filter_by(path=self.path).first())
        finally:
            session.close()

    def test_tv_detected_from_path(self):
        with tempfile.TemporaryDirectory() as d:
            tv_dir = os.path.join(d, "TvShows", "Some Show", "Season 1")
            os.makedirs(tv_dir)
            tv_path = os.path.join(tv_dir, "1x01 - Pilot.mkv")
            with open(tv_path, "w") as f:
                f.write("data")
            try:
                with patch("media_manager.scanner.get_video_metadata", return_value=FAKE_METADATA):
                    process_file(tv_path, storage_volume=d)
                session = SyncSessionLocal()
                try:
                    item = session.query(MediaItem).filter_by(path=tv_path).first()
                    self.assertIsNotNone(item)
                    self.assertEqual(item.media_type, "tv")
                finally:
                    session.close()
            finally:
                session = SyncSessionLocal()
                try:
                    session.query(MediaItem).filter_by(path=tv_path).delete()
                    session.commit()
                finally:
                    session.close()

    def test_metadata_extraction_exception_is_caught(self):
        with patch("media_manager.scanner.get_video_metadata", side_effect=RuntimeError("boom")):
            process_file(self.path, storage_volume="/tmp")  # must not raise

        session = SyncSessionLocal()
        try:
            self.assertIsNone(session.query(MediaItem).filter_by(path=self.path).first())
        finally:
            session.close()


class TestProcessFileRichDetails(unittest.TestCase):
    """process_file persists metadata.extract_details() output on the row."""

    setUp = TestProcessFile.setUp
    tearDown = TestProcessFile.tearDown

    DETAILS = {
        "video_profile": "High", "pix_fmt": "yuv420p", "bit_depth": 8, "hdr_format": "sdr",
        "dv_profile": None, "color_transfer": "bt709", "color_primaries": "bt709",
        "field_order": "progressive", "frame_rate": 23.976, "video_bitrate": 4_500_000,
        "container": "matroska,webm", "duration_seconds": 100.0, "audio_stream_count": 2,
        "subtitle_stream_count": 3, "attachment_count": 0,
    }

    def _row(self):
        session = SyncSessionLocal()
        try:
            return session.query(MediaItem).filter_by(path=self.path).first()
        finally:
            session.close()

    def test_new_row_gets_details_and_returns_true(self):
        meta = dict(FAKE_METADATA, details=self.DETAILS)
        with patch("media_manager.scanner.get_video_metadata", return_value=meta):
            self.assertTrue(process_file(self.path, storage_volume="/tmp"))
        item = self._row()
        self.assertEqual(item.hdr_format, "sdr")
        self.assertEqual(item.video_bitrate, 4_500_000)
        self.assertEqual(item.subtitle_stream_count, 3)
        self.assertEqual(item.probe_version, PROBE_VERSION)
        self.assertIsNotNone(item.probed_at)

    def test_score_uses_codec_and_video_bitrate(self):
        h264 = dict(FAKE_METADATA, bit_rate=6_000_000, details=dict(self.DETAILS, video_bitrate=5_000_000))
        with patch("media_manager.scanner.get_video_metadata", return_value=h264):
            process_file(self.path, storage_volume="/tmp")
        h264_score = self._row().quality_score
        hevc = dict(h264, video_codec="hevc")
        with patch("media_manager.scanner.get_video_metadata", return_value=hevc):
            process_file(self.path, storage_volume="/tmp")
        # Only the 5 Mbps video part counts double: 6 -> 11 Mbps equivalent = +25 pts.
        self.assertEqual(self._row().quality_score, h264_score + 25)

    def test_skips_in_flight_transcode_output(self):
        with patch("media_manager.web.services.transcode_jobs.swap_locked_sync",
                   return_value={"item_ids": set(), "output_paths": {self.path}}), \
             patch("media_manager.scanner.get_video_metadata") as probe:
            self.assertIsNone(process_file(self.path, storage_volume="/tmp"))
        probe.assert_not_called()
        self.assertIsNone(self._row())
        with patch("media_manager.web.services.transcode_jobs.swap_locked_sync", side_effect=RuntimeError("x")), \
             patch("media_manager.scanner.get_video_metadata", return_value=FAKE_METADATA):
            process_file(self.path, storage_volume="/tmp")  # lock lookup failure: index normally
        self.assertIsNotNone(self._row())

    def test_existing_row_details_updated(self):
        with patch("media_manager.scanner.get_video_metadata", return_value=FAKE_METADATA):
            self.assertFalse(process_file(self.path, storage_volume="/tmp"))  # no details: not a full probe
        self.assertIsNone(self._row().probe_version)
        meta = dict(FAKE_METADATA, details=dict(self.DETAILS, hdr_format="hdr10", bit_depth=10))
        with patch("media_manager.scanner.get_video_metadata", return_value=meta):
            self.assertTrue(process_file(self.path, storage_volume="/tmp"))
        item = self._row()
        self.assertEqual(item.hdr_format, "hdr10")
        self.assertEqual(item.bit_depth, 10)
        self.assertEqual(item.probe_version, PROBE_VERSION)


class TestScanAll(unittest.TestCase):
    def setUp(self):
        self.scan_dir = tempfile.mkdtemp(prefix="media_manager_scan_test_")
        self.movie_path = os.path.join(self.scan_dir, "Some Movie (2020).mkv")
        with open(self.movie_path, "w") as f:
            f.write("data")
        self.other_ext_path = os.path.join(self.scan_dir, "readme.txt")
        with open(self.other_ext_path, "w") as f:
            f.write("not media")

    def tearDown(self):
        shutil.rmtree(self.scan_dir, ignore_errors=True)
        session = SyncSessionLocal()
        try:
            session.query(MonitoredDirectory).filter_by(path=self.scan_dir).delete()
            session.query(MediaItem).filter_by(path=self.movie_path).delete()
            session.commit()
        finally:
            session.close()

    def test_scans_monitored_directory_and_indexes_matching_files(self):
        session = SyncSessionLocal()
        try:
            session.add(MonitoredDirectory(path=self.scan_dir))
            session.commit()
        finally:
            session.close()

        with patch("media_manager.scanner.get_video_metadata", return_value=FAKE_METADATA):
            scan_all()

        session = SyncSessionLocal()
        try:
            item = session.query(MediaItem).filter_by(path=self.movie_path).first()
            self.assertIsNotNone(item)
            no_txt = session.query(MediaItem).filter_by(path=self.other_ext_path).first()
            self.assertIsNone(no_txt)
        finally:
            session.close()

    def test_falls_back_to_default_media_directories_when_none_monitored(self):
        session = SyncSessionLocal()
        try:
            session.query(MonitoredDirectory).delete()
            session.commit()
        finally:
            session.close()

        with patch("media_manager.scanner.MEDIA_DIRECTORIES", [self.scan_dir]), \
             patch("media_manager.scanner.get_video_metadata", return_value=FAKE_METADATA):
            scan_all()

        session = SyncSessionLocal()
        try:
            item = session.query(MediaItem).filter_by(path=self.movie_path).first()
            self.assertIsNotNone(item)
        finally:
            session.close()

    def test_skips_nonexistent_directory(self):
        session = SyncSessionLocal()
        try:
            session.add(MonitoredDirectory(path="/tmp/_ut_scanner_dir_does_not_exist_"))
            session.commit()
        finally:
            session.close()
        try:
            scan_all()  # must not raise
        finally:
            session = SyncSessionLocal()
            try:
                session.query(MonitoredDirectory).filter_by(path="/tmp/_ut_scanner_dir_does_not_exist_").delete()
                session.commit()
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
