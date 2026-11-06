import copy
import unittest

from media_manager.transcode.verify import compare

SRC = {"format": {"duration": "1000.0"}, "streams": [
    {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "nb_read_packets": "24000"},
    {"index": 1, "codec_type": "audio", "codec_name": "ac3", "tags": {"language": "eng"}},
    {"index": 2, "codec_type": "subtitle", "codec_name": "mov_text", "tags": {"language": "eng"}},
    {"index": 3, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
]}
OUT = {"format": {"duration": "1000.4"}, "streams": [
    {"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080,
     "pix_fmt": "yuv420p10le", "nb_read_packets": "24001"},
    {"index": 1, "codec_type": "audio", "codec_name": "ac3", "tags": {"language": "eng"}},
    {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
]}
PLAN = {"ten_bit": True, "scale_width": None}


def _check(out=None, plan=PLAN, src_size=1000, out_size=500, decode_errors=None, src=SRC):
    return compare(src, out if out is not None else OUT, plan, src_size, out_size, 10, decode_errors)


class TestCompare(unittest.TestCase):
    def test_good_transcode_passes(self):
        self.assertEqual(_check(), (True, []))

    def test_undetermined_language_matches_missing_tag(self):
        src = copy.deepcopy(SRC)
        src["streams"][1]["tags"]["language"] = "und"
        out = copy.deepcopy(OUT)
        out["streams"][1].pop("tags")
        self.assertEqual(_check(out, src=src), (True, []))

    def _broken(self, mutate):
        out = copy.deepcopy(OUT)
        mutate(out)
        return _check(out)[1]

    def test_each_failure(self):
        self.assertIn("duration_mismatch", self._broken(lambda o: o["format"].update(duration="990")))
        self.assertIn("audio_count_mismatch", self._broken(lambda o: o["streams"].pop(1)))
        self.assertIn("audio_stream_changed", self._broken(lambda o: o["streams"][1].update(codec_name="aac")))
        self.assertIn("subtitle_stream_changed", self._broken(lambda o: o["streams"][2]["tags"].update(language="fre")))
        self.assertIn("video_not_hevc", self._broken(lambda o: o["streams"][0].update(codec_name="h264")))
        self.assertIn("video_size_mismatch", self._broken(lambda o: o["streams"][0].update(width=1280)))
        self.assertIn("video_not_10bit", self._broken(lambda o: o["streams"][0].update(pix_fmt="yuv420p")))
        self.assertIn("frame_count_mismatch", self._broken(lambda o: o["streams"][0].update(nb_read_packets="23000")))
        self.assertIn("frame_count_mismatch", self._broken(lambda o: o["streams"][0].pop("nb_read_packets")))
        self.assertIn("video_not_hevc", self._broken(lambda o: o["streams"].pop(0)))
        self.assertIn("duration_mismatch", self._broken(lambda o: o["format"].update(duration="n/a")))

    def test_size_and_decode_errors(self):
        self.assertEqual(_check(out_size=950)[1], ["not_smaller"])  # needs 10% savings
        self.assertEqual(_check(out_size=0)[1], ["not_smaller"])
        self.assertEqual(_check(decode_errors="error while decoding")[1], ["output_decode_errors"])

    def test_downscale_checks_width_only_and_packets_optional(self):
        out = copy.deepcopy(OUT)
        out["streams"][0].update(width=1920, height=800)
        src = copy.deepcopy(SRC)
        src["streams"][0].update(width=3840, height=1600)
        self.assertEqual(_check(out, plan={"ten_bit": True, "scale_width": 1920}, src=src)[1], [])
        src["streams"][0].pop("nb_read_packets")
        self.assertEqual(_check(out, plan={"ten_bit": False, "scale_width": 1280}, src=src)[1], ["video_size_mismatch"])


if __name__ == "__main__":
    unittest.main()
