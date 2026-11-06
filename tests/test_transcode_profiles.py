import ast
import os
import unittest

from media_manager.transcode import profiles
from media_manager.transcode.profiles import DEFAULTS, DEFAULT_SOURCE_CODECS, decide, resolution_bucket


def _item(**kw):
    base = {"path": "/media/Movies/M (2000)/M (2000).mkv", "media_type": "movie", "video_codec": "h264",
            "height": 1080, "width": 1920, "hdr_format": "sdr", "bit_depth": 8, "pix_fmt": "yuv420p",
            "field_order": "progressive", "video_bitrate": 10_000_000, "bitrate": 11_000_000,
            "size_bytes": 8_000_000_000, "duration_seconds": 6000, "probe_version": 1}
    base.update(kw)
    return base


def _decide(item, media_type="movie", codecs=DEFAULT_SOURCE_CODECS, excluded=(), **setting_overrides):
    settings = dict(DEFAULTS[media_type], **setting_overrides)
    return decide(item, settings, codecs, excluded)


class TestResolutionBucket(unittest.TestCase):
    def test_buckets_are_width_aware(self):
        self.assertEqual(resolution_bucket(2160, 3840), "2160")
        self.assertEqual(resolution_bucket(1600, 3840), "2160")
        self.assertEqual(resolution_bucket(800, 1920), "1080")  # 2.40:1 scope 1080p
        self.assertEqual(resolution_bucket(1080, 1920), "1080")
        self.assertEqual(resolution_bucket(720, 1280), "720")
        self.assertEqual(resolution_bucket(534, 1280), "720")
        self.assertEqual(resolution_bucket(480, 720), "sd")
        self.assertEqual(resolution_bucket(None, None), "sd")


class TestDecide(unittest.TestCase):
    def test_sdr_1080p_h264_movie_is_eligible_with_movie_plan(self):
        d = _decide(_item())
        self.assertTrue(d.eligible)
        self.assertEqual(d.bucket, "1080")
        self.assertEqual(d.plan, {"cq": 22, "preset": "p7", "multipass": "fullres", "ten_bit": True,
                                  "scale_width": None, "decode": "cuda", "deinterlace": False,
                                  "max_video_bps": 10_000_000})

    def test_tv_uses_tv_profile(self):
        d = _decide(_item(media_type="tv"), "tv")
        self.assertEqual((d.plan["cq"], d.plan["preset"], d.plan["multipass"]), (25, "p5", "qres"))

    def test_skip_reasons_in_priority_order(self):
        cases = [
            (_item(size_bytes=0), "empty_file"),
            (_item(probe_version=None), "not_probed"),
            (_item(video_codec="hevc"), "already_hevc"),
            (_item(video_codec="vc1"), "codec_excluded"),
            (_item(hdr_format="dv"), "hdr"),
            (_item(hdr_format="hdr10"), "hdr"),
            (_item(height=2160, width=3840), "4k"),
            (_item(field_order="tt"), "interlaced"),
            (_item(pix_fmt="yuv422p"), "unsupported_pix_fmt"),
            (_item(video_bitrate=3_000_000), "low_bitrate"),
        ]
        for item, reason in cases:
            with self.subTest(reason=reason):
                d = _decide(item)
                self.assertFalse(d.eligible)
                self.assertEqual(d.reason, reason)
                self.assertIn(reason, profiles.SKIP_REASONS)

    def test_excluded_path_and_disabled_type(self):
        self.assertEqual(_decide(_item(), excluded=["/media/Movies/"]).reason, "excluded_path")
        self.assertEqual(_decide(_item(), excluded=[""]).eligible, True)  # blank prefix ignored
        self.assertEqual(_decide(_item(), enabled=False).reason, "type_disabled")

    def test_tv_4k_sdr_is_downscaled(self):
        d = _decide(_item(media_type="tv", height=2160, width=3840, video_bitrate=15_000_000), "tv")
        self.assertTrue(d.eligible)
        self.assertEqual(d.bucket, "4k_down")
        self.assertEqual(d.plan["scale_width"], 1920)
        self.assertEqual(d.plan["cq"], DEFAULTS["tv"]["cq_4k_downscale"])

    def test_tv_4k_hdr_still_skipped(self):
        self.assertEqual(_decide(_item(media_type="tv", height=2160, width=3840, hdr_format="hdr10"), "tv").reason, "hdr")

    def test_hdr_allowed_when_skip_disabled(self):
        self.assertTrue(_decide(_item(hdr_format="hlg"), skip_hdr=False).eligible)

    def test_interlaced_allowed_sets_deinterlace(self):
        d = _decide(_item(field_order="bb"), allow_interlaced=True)
        self.assertTrue(d.plan["deinterlace"])

    def test_software_decode_for_xvid_and_hi10p(self):
        self.assertEqual(_decide(_item(video_codec="mpeg4", height=480, width=720, video_bitrate=1_500_000)).plan["decode"], "sw")
        self.assertEqual(_decide(_item(pix_fmt="yuv420p10le", bit_depth=10)).plan["decode"], "sw")

    def test_falls_back_to_container_bitrate_and_codec_case(self):
        d = _decide(_item(video_bitrate=None, bitrate=9_000_000, video_codec="H264"))
        self.assertTrue(d.eligible)
        self.assertEqual(d.plan["max_video_bps"], 9_000_000)

    def test_custom_source_codecs(self):
        self.assertTrue(_decide(_item(video_codec="vc1", pix_fmt="yuv420p"), codecs=["vc1"]).eligible)
        self.assertEqual(_decide(_item(), codecs=["vc1"]).reason, "codec_excluded")


class TestPurity(unittest.TestCase):
    """The host worker imports media_manager.transcode without the app's DB/web stack."""

    FORBIDDEN = ("media_manager.database", "media_manager.models", "media_manager.web", "sqlalchemy", "fastapi", "httpx")

    def test_package_imports_no_db_or_web_modules(self):
        pkg = os.path.dirname(profiles.__file__)
        for name in os.listdir(pkg):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(pkg, name)) as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                for mod in mods:
                    with self.subTest(file=name, module=mod):
                        self.assertFalse(mod.startswith(self.FORBIDDEN), f"{name} imports {mod}")


if __name__ == "__main__":
    unittest.main()
