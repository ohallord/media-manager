import unittest

from media_manager.transcode.estimate import (
    DEFAULT_RATIOS, MIN_CALIBRATION_SAMPLES, calibrate, codec_family, estimate_output_bytes,
)


def _item(**kw):
    base = {"media_type": "movie", "video_codec": "h264", "size_bytes": 7_500_000_000,
            "duration_seconds": 6000, "bitrate": 10_000_000, "video_bitrate": 9_000_000}
    base.update(kw)
    return base


class TestEstimate(unittest.TestCase):
    def test_scales_video_keeps_audio(self):
        # 9 Mbps video * 0.55 = 4.95 Mbps, + 1 Mbps audio, over 6000 s.
        self.assertEqual(estimate_output_bytes(_item(), "1080"), int((4_950_000 + 1_000_000) * 6000 / 8))

    def test_cap_limits_bloated_sources(self):
        out = estimate_output_bytes(_item(media_type="tv", video_bitrate=20_000_000, bitrate=21_000_000,
                                          size_bytes=20_000_000_000), "1080")
        self.assertEqual(out, int((3_000_000 + 1_000_000) * 6000 / 8))

    def test_never_bigger_than_source(self):
        self.assertEqual(estimate_output_bytes(_item(size_bytes=100), "1080"), 100)

    def test_fallbacks(self):
        self.assertEqual(estimate_output_bytes(_item(duration_seconds=None), "1080"), int(7_500_000_000 * 0.55))
        # unknown video split: assume 85% video
        out = estimate_output_bytes(_item(video_bitrate=None), "720")
        self.assertEqual(out, int(min(8_500_000 * 0.55, 4_000_000) * 6000 / 8 + 1_500_000 * 6000 / 8))
        self.assertEqual(estimate_output_bytes(_item(video_codec="mpeg4", media_type="tv", duration_seconds=0), "sd"),
                         int(7_500_000_000 * 0.5))
        self.assertEqual(estimate_output_bytes(_item(video_codec="weird", duration_seconds=0), "zzz"), int(7_500_000_000 * 0.55))

    def test_codec_family(self):
        self.assertEqual(codec_family("msmpeg4v3"), "mpeg4")
        self.assertEqual(codec_family("MPEG4"), "mpeg4")
        self.assertEqual(codec_family(None), "")

    def test_calibrate_needs_enough_samples(self):
        key = ("h264", "tv")
        few = [(key, 0.3)] * (MIN_CALIBRATION_SAMPLES - 1)
        self.assertEqual(calibrate(few)[key], DEFAULT_RATIOS[key])
        many = [(key, v) for v in (0.3, 0.32, 0.35, 0.4, 0.9)] + [(key, 0), (key, None)]
        self.assertEqual(calibrate(many)[key], 0.35)
        self.assertEqual(calibrate(many)[("h264", "movie")], DEFAULT_RATIOS[("h264", "movie")])


if __name__ == "__main__":
    unittest.main()
