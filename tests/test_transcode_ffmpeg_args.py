import unittest

from media_manager.transcode.ffmpeg_args import (
    STALE_STAT_TAGS, build_encode_args, build_verify_decode_args, parse_progress_line,
    progress_fraction, stream_plan,
)

PLAN = {"cq": 20, "preset": "p7", "multipass": "fullres", "ten_bit": True, "scale_width": None,
        "decode": "cuda", "deinterlace": False, "max_video_bps": 10_000_000}


def _probe(streams, fmt="matroska,webm"):
    return {"format": {"format_name": fmt, "duration": "100"}, "streams": streams}


MKV = _probe([
    {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
    {"index": 1, "codec_type": "audio", "codec_name": "dts"},
    {"index": 2, "codec_type": "audio", "codec_name": "ac3"},
    {"index": 3, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
    {"index": 4, "codec_type": "attachment", "codec_name": "ttf"},
])

MP4 = _probe([
    {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
    {"index": 1, "codec_type": "audio", "codec_name": "aac"},
    {"index": 2, "codec_type": "subtitle", "codec_name": "mov_text"},
    {"index": 3, "codec_type": "data", "codec_name": "bin_data"},
    {"index": 4, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
], fmt="mov,mp4,m4a,3gp,3g2,mj2")

AVI = _probe([
    {"index": 0, "codec_type": "video", "codec_name": "mpeg4", "width": 720, "height": 480},
    {"index": 1, "codec_type": "audio", "codec_name": "mp3"},
], fmt="avi")


def _tail(args):
    tags = []
    for tag in STALE_STAT_TAGS:
        tags += ["-metadata:s:v:0", f"{tag}="]
    return tags + ["-fps_mode", "passthrough", "-max_muxing_queue_size", "4096", "-f", "matroska"]


NVENC = ["-c:v", "hevc_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", "20", "-b:v", "0",
         "-maxrate", "10000000", "-bufsize", "20000000", "-multipass", "fullres", "-spatial_aq", "1",
         "-temporal_aq", "1", "-aq-strength", "8", "-rc-lookahead", "32", "-bf", "4",
         "-b_ref_mode", "middle", "-g", "240", "-profile:v", "main10"]
HEAD = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-nostats", "-progress", "pipe:1"]


class TestBuildEncodeArgs(unittest.TestCase):
    def test_golden_h264_mkv_movie_full_gpu_pipeline(self):
        args = build_encode_args(MKV, PLAN, "/in.mkv", "/out.mkv")
        expected = (HEAD + ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", "/in.mkv",
                            "-map", "0:0", "-map", "0:1", "-map", "0:2", "-map", "0:3", "-map", "0:4",
                            "-map_metadata", "0", "-map_chapters", "0", "-c", "copy",
                            "-vf", "scale_cuda=format=p010le"] + NVENC + _tail(None) + ["/out.mkv"])
        self.assertEqual(args, expected)

    def test_golden_tv_4k_downscale(self):
        plan = dict(PLAN, scale_width=1920, cq=23, preset="p6", multipass="qres")
        args = build_encode_args(MKV, plan, "/in.mkv", "/out.mkv")
        self.assertIn("scale_cuda=w=1920:h=-2:interp_algo=lanczos:format=p010le", args)
        self.assertEqual(args[args.index("-preset") + 1], "p6")

    def test_mp4_drops_cover_art_and_data_converts_mov_text(self):
        args = build_encode_args(MP4, PLAN, "/in.mp4", "/out.mkv")
        maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
        self.assertEqual(maps, ["0:0", "0:1", "0:2"])  # no data track, no cover art
        self.assertEqual(args[args.index("-c:s:0") + 1], "srt")
        self.assertEqual(stream_plan(MP4)["dropped"], ["data:bin_data#3", "cover_art:mjpeg#4"])

    def test_avi_software_decode_genpts(self):
        plan = dict(PLAN, decode="sw", cq=24)
        args = build_encode_args(AVI, plan, "/in.avi", "/out.mkv")
        self.assertEqual(args[args.index("-fflags") + 1], "+genpts")
        self.assertNotIn("-hwaccel", args)
        self.assertEqual(args[args.index("-vf") + 1], "format=p010le")

    def test_force_sw_decode_and_deinterlace_and_8bit(self):
        plan = dict(PLAN, deinterlace=True, ten_bit=False, scale_width=1920)
        sw = build_encode_args(MKV, plan, "/a", "/b", force_sw_decode=True)
        self.assertEqual(sw[sw.index("-vf") + 1], "bwdif=mode=send_frame,scale=1920:-2:flags=lanczos,format=yuv420p")
        self.assertEqual(sw[sw.index("-profile:v") + 1], "main")
        hw = build_encode_args(MKV, plan, "/a", "/b")
        self.assertEqual(hw[hw.index("-vf") + 1],
                         "bwdif_cuda=mode=send_frame,scale_cuda=w=1920:h=-2:interp_algo=lanczos:format=nv12")

    def test_libx265_for_ci_and_no_maxrate_when_unknown(self):
        args = build_encode_args(MKV, dict(PLAN, max_video_bps=None), "/a", "/b", encoder="libx265")
        self.assertNotIn("-hwaccel", args)  # cuda frames only feed NVENC
        self.assertEqual(args[args.index("-vf") + 1], "format=yuv420p10le")
        self.assertEqual(args[args.index("-c:v") + 1], "libx265")
        self.assertEqual(args[args.index("-crf") + 1], "20")
        nv = build_encode_args(MKV, dict(PLAN, max_video_bps=None), "/a", "/b")
        self.assertNotIn("-maxrate", nv)

    def test_sample_window(self):
        args = build_encode_args(MKV, PLAN, "/a", "/b", start=300, duration=20)
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "-map"], ["0:0"])  # video only
        self.assertLess(args.index("-ss"), args.index("-i"))
        self.assertEqual(args[args.index("-ss") + 1], "300.000")
        self.assertGreater(args.index("-t"), args.index("-i"))
        self.assertEqual(args[args.index("-t") + 1], "20.000")

    def test_no_video_raises(self):
        with self.assertRaises(ValueError):
            build_encode_args(_probe([{"index": 0, "codec_type": "audio"}]), PLAN, "/a", "/b")


class TestHelpers(unittest.TestCase):
    def test_verify_decode_args(self):
        self.assertEqual(build_verify_decode_args("/o.mkv", ffmpeg="/usr/bin/ffmpeg"),
                         ["/usr/bin/ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", "/o.mkv",
                          "-map", "0:v:0", "-f", "null", "-"])

    def test_progress_parsing(self):
        self.assertEqual(parse_progress_line("out_time_us=5000000\n"), ("out_time_us", "5000000"))
        self.assertIsNone(parse_progress_line("garbage"))
        self.assertEqual(progress_fraction("5000000", 10), 0.5)
        self.assertEqual(progress_fraction("50000000", 10), 1.0)
        self.assertIsNone(progress_fraction("N/A", 10))
        self.assertIsNone(progress_fraction("5", 0))
        self.assertIsNone(progress_fraction("-1", 10))


if __name__ == "__main__":
    unittest.main()
