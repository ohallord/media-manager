import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from media_manager.metadata import (
    DETAIL_FIELDS,
    FFPROBE_TIMEOUT_SECONDS,
    PROBE_VERSION,
    apply_probe_details,
    extract_details,
    get_video_metadata,
    main_video_stream,
    run_ffprobe,
)


def _ffprobe_json(video=True, audio=True, height=1080, width=1920, duration="120.5",
                   size="1000000", bit_rate="5000000", video_codec="h264",
                   audio_codec="aac", channels=2):
    streams = []
    if video:
        streams.append({
            "codec_type": "video", "codec_name": video_codec,
            "width": width, "height": height,
        })
    if audio:
        streams.append({
            "codec_type": "audio", "codec_name": audio_codec, "channels": channels,
        })
    return json.dumps({
        "format": {
            "format_name": "matroska,webm",
            "duration": duration,
            "size": size,
            "bit_rate": bit_rate,
        },
        "streams": streams,
    })


class TestGetVideoMetadata(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".mkv", delete=False)
        self.tmp.write(b"fake video bytes")
        self.tmp.close()
        self.path = self.tmp.name

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _run_with_stdout(self, stdout_json):
        fake_result = MagicMock(stdout=stdout_json)
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            result = get_video_metadata(self.path)
        mock_run.assert_called_once()
        return result

    def test_4k_video_with_audio(self):
        meta = self._run_with_stdout(_ffprobe_json(height=2160, width=3840))
        self.assertEqual(meta["resolution_label"], "4K")
        self.assertEqual(meta["video_codec"], "h264")
        self.assertEqual(meta["audio_codec"], "aac")
        self.assertEqual(meta["audio_channels"], 2)
        self.assertEqual(meta["height"], 2160)
        self.assertEqual(meta["width"], 3840)

    def test_1080p_resolution_label(self):
        meta = self._run_with_stdout(_ffprobe_json(height=1080))
        self.assertEqual(meta["resolution_label"], "1080p")

    def test_720p_resolution_label(self):
        meta = self._run_with_stdout(_ffprobe_json(height=720))
        self.assertEqual(meta["resolution_label"], "720p")

    def test_480p_resolution_label(self):
        meta = self._run_with_stdout(_ffprobe_json(height=480))
        self.assertEqual(meta["resolution_label"], "480p")

    def test_sd_resolution_label(self):
        meta = self._run_with_stdout(_ffprobe_json(height=240))
        self.assertEqual(meta["resolution_label"], "SD")

    def test_audio_only_mp3_labeled_audio(self):
        mp3_tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        mp3_tmp.close()
        try:
            fake_result = MagicMock(stdout=_ffprobe_json(video=False, audio=True, height=0, width=0))
            with patch("subprocess.run", return_value=fake_result):
                meta = get_video_metadata(mp3_tmp.name)
            self.assertEqual(meta["resolution_label"], "Audio")
            self.assertIsNone(meta["video_codec"])
        finally:
            os.remove(mp3_tmp.name)

    def test_no_streams_labeled_unknown(self):
        meta = self._run_with_stdout(_ffprobe_json(video=False, audio=False, height=0, width=0))
        self.assertEqual(meta["resolution_label"], "Unknown")

    def test_ffprobe_called_process_error_returns_fallback(self):
        err = subprocess.CalledProcessError(1, ["ffprobe"], output="", stderr="EBML header parsing failed")
        with patch("subprocess.run", side_effect=err):
            meta = get_video_metadata(self.path)
        self.assertEqual(meta["video_codec"], "unknown")
        self.assertEqual(meta["resolution_label"], "Unknown")
        self.assertEqual(meta["format_name"], "mkv")
        self.assertGreater(meta["size"], 0)

    def test_ffprobe_called_process_error_no_stderr(self):
        err = subprocess.CalledProcessError(1, ["ffprobe"], output="", stderr=None)
        with patch("subprocess.run", side_effect=err):
            meta = get_video_metadata(self.path)
        self.assertEqual(meta["resolution_label"], "Unknown")

    def test_generic_exception_returns_fallback(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("ffprobe missing")):
            meta = get_video_metadata(self.path)
        self.assertEqual(meta["video_codec"], "unknown")
        self.assertEqual(meta["resolution_label"], "Unknown")

    def test_missing_bitrate_defaults_to_zero(self):
        meta = self._run_with_stdout(_ffprobe_json(bit_rate=None if False else "0"))
        self.assertEqual(meta["bit_rate"], 0)


if __name__ == "__main__":
    unittest.main()


def _probe(streams, fmt=None):
    return {"format": fmt if fmt is not None else {"format_name": "matroska,webm", "duration": "100.5"},
            "streams": streams}


def _video(**overrides):
    stream = {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
              "profile": "High", "pix_fmt": "yuv420p", "avg_frame_rate": "24000/1001",
              "field_order": "progressive", "color_transfer": "bt709", "color_primaries": "bt709"}
    stream.update(overrides)
    return stream


COVER_ART = {"codec_type": "video", "codec_name": "mjpeg", "width": 600, "height": 900,
             "disposition": {"attached_pic": 1}}


class TestExtractDetails(unittest.TestCase):
    def test_sdr_h264_basic_fields(self):
        d = extract_details(_probe([_video(bit_rate="4000000"), {"codec_type": "audio"},
                                    {"codec_type": "audio"}, {"codec_type": "subtitle"},
                                    {"codec_type": "attachment"}]))
        self.assertEqual(d["hdr_format"], "sdr")
        self.assertIsNone(d["dv_profile"])
        self.assertEqual(d["video_profile"], "High")
        self.assertEqual(d["pix_fmt"], "yuv420p")
        self.assertEqual(d["bit_depth"], 8)
        self.assertEqual(d["frame_rate"], 23.976)
        self.assertEqual(d["field_order"], "progressive")
        self.assertEqual(d["video_bitrate"], 4_000_000)
        self.assertEqual(d["container"], "matroska,webm")
        self.assertEqual(d["duration_seconds"], 100.5)
        self.assertEqual(d["audio_stream_count"], 2)
        self.assertEqual(d["subtitle_stream_count"], 1)
        self.assertEqual(d["attachment_count"], 1)
        self.assertEqual(set(d), set(DETAIL_FIELDS))

    def test_dolby_vision_from_side_data(self):
        d = extract_details(_probe([_video(
            codec_name="hevc", pix_fmt="yuv420p10le", color_transfer="smpte2084",
            side_data_list=[{"side_data_type": "DOVI configuration record", "dv_profile": 8}],
        )]))
        self.assertEqual(d["hdr_format"], "dv")
        self.assertEqual(d["dv_profile"], 8)
        self.assertEqual(d["bit_depth"], 10)

    def test_hdr10_and_hlg_from_color_transfer(self):
        self.assertEqual(extract_details(_probe([_video(color_transfer="smpte2084")]))["hdr_format"], "hdr10")
        self.assertEqual(extract_details(_probe([_video(color_transfer="arib-std-b67")]))["hdr_format"], "hlg")

    def test_unrelated_side_data_is_not_dv(self):
        d = extract_details(_probe([_video(side_data_list=[{"side_data_type": "Display Matrix"}])]))
        self.assertEqual(d["hdr_format"], "sdr")

    def test_cover_art_is_not_the_main_video(self):
        d = extract_details(_probe([COVER_ART, _video(codec_name="h264", profile="Main")]))
        self.assertEqual(d["video_profile"], "Main")
        self.assertIsNone(main_video_stream(_probe([COVER_ART])))

    def test_only_cover_art_means_no_video_fields(self):
        d = extract_details(_probe([COVER_ART, {"codec_type": "audio"}]))
        self.assertIsNone(d["hdr_format"])
        self.assertIsNone(d["bit_depth"])
        self.assertEqual(d["audio_stream_count"], 1)

    def test_bitrate_from_mkv_bps_tags(self):
        self.assertEqual(extract_details(_probe([_video(tags={"BPS": "3500000"})]))["video_bitrate"], 3_500_000)
        self.assertEqual(extract_details(_probe([_video(tags={"BPS-eng": "2500000"})]))["video_bitrate"], 2_500_000)
        self.assertIsNone(extract_details(_probe([_video(tags={"BPS": "n/a", "title": "x"})]))["video_bitrate"])
        self.assertIsNone(extract_details(_probe([_video()]))["video_bitrate"])

    def test_bit_depth_sources(self):
        self.assertEqual(extract_details(_probe([_video(bits_per_raw_sample="10")]))["bit_depth"], 10)
        self.assertEqual(extract_details(_probe([_video(pix_fmt="yuv420p12le")]))["bit_depth"], 12)
        self.assertEqual(extract_details(_probe([_video(pix_fmt="p010le")]))["bit_depth"], 10)
        self.assertIsNone(extract_details(_probe([_video(pix_fmt=None)]))["bit_depth"])

    def test_frame_rate_fallbacks(self):
        self.assertEqual(extract_details(_probe([_video(avg_frame_rate="0/0", r_frame_rate="25/1")]))["frame_rate"], 25.0)
        self.assertIsNone(extract_details(_probe([_video(avg_frame_rate="bogus", r_frame_rate="1/0")]))["frame_rate"])
        self.assertIsNone(extract_details(_probe([_video(avg_frame_rate=None)]))["frame_rate"])

    def test_interlaced_field_order_kept(self):
        self.assertEqual(extract_details(_probe([_video(field_order="tt")]))["field_order"], "tt")

    def test_missing_format_section(self):
        d = extract_details({"streams": [_video()]})
        self.assertIsNone(d["container"])
        self.assertIsNone(d["duration_seconds"])


class TestApplyProbeDetails(unittest.TestCase):
    def test_copies_fields_and_stamps_version(self):
        target = MagicMock()
        apply_probe_details(target, {"hdr_format": "hdr10", "bit_depth": 10})
        self.assertEqual(target.hdr_format, "hdr10")
        self.assertEqual(target.bit_depth, 10)
        self.assertIsNone(target.container)  # absent keys are written as None
        self.assertEqual(target.probe_version, PROBE_VERSION)
        self.assertIsNotNone(target.probed_at)

    def test_none_details_leave_target_untouched(self):
        class Target:
            pass
        target = Target()
        apply_probe_details(target, None)
        self.assertFalse(hasattr(target, "probe_version"))


class TestRunFfprobe(unittest.TestCase):
    def test_passes_timeout_and_parses_json(self):
        fake = MagicMock(stdout='{"streams": [], "format": {}}')
        with patch("subprocess.run", return_value=fake) as mock_run:
            self.assertEqual(run_ffprobe("/x.mkv"), {"streams": [], "format": {}})
        self.assertEqual(mock_run.call_args.kwargs["timeout"], FFPROBE_TIMEOUT_SECONDS)
        self.assertEqual(mock_run.call_args.args[0][-1], "/x.mkv")


class TestGetVideoMetadataRich(unittest.TestCase):
    setUp = TestGetVideoMetadata.setUp
    tearDown = TestGetVideoMetadata.tearDown
    _run_with_stdout = TestGetVideoMetadata._run_with_stdout

    def test_skips_cover_art_for_core_fields(self):
        payload = json.dumps(_probe([COVER_ART, _video(codec_name="h264", width=1280, height=720),
                                     {"codec_type": "audio", "codec_name": "aac", "channels": 2}]))
        meta = self._run_with_stdout(payload)
        self.assertEqual(meta["video_codec"], "h264")
        self.assertEqual(meta["height"], 720)
        self.assertEqual(meta["resolution_label"], "720p")

    def test_includes_details(self):
        meta = self._run_with_stdout(_ffprobe_json())
        self.assertEqual(meta["details"]["container"], "matroska,webm")
        self.assertEqual(meta["details"]["audio_stream_count"], 1)

    def test_timeout_returns_fallback_without_details(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["ffprobe"], 120)):
            meta = get_video_metadata(self.path)
        self.assertEqual(meta["video_codec"], "unknown")
        self.assertNotIn("details", meta)
