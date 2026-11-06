"""Which files to transcode, and with what encode plan (pure)."""
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional

# ffprobe codec_name values converted by default (h264 + the MPEG-4 ASP family:
# XviD/DivX report as mpeg4, old DivX 3 as msmpeg4v3).
DEFAULT_SOURCE_CODECS = ("h264", "mpeg4", "msmpeg4v3", "msmpeg4v2", "msmpeg4v1")

# Per-media-type defaults; TranscodeSettings rows are created from these.
# Tuned on library samples (RTX 5060 Ti, 1080p h264): NVENC CQ 20 saved ~43%,
# CQ 24 ~65%, CQ 27 ~76%; TV p5 matched p7's size at 2.7x the speed.
# CQ: lower = better quality. min_kbps_*: below this video bitrate a file is
# already small and re-encoding saves too little to bother.
DEFAULTS: Dict[str, Dict[str, Any]] = {
    "movie": {
        "enabled": True,
        "cq_1080": 22, "cq_720": 23, "cq_sd": 23, "cq_4k_downscale": 22,
        "preset": "p7", "multipass": "fullres", "output_10bit": True,
        "min_savings_pct": 10,
        "min_kbps_1080": 4000, "min_kbps_720": 2000, "min_kbps_sd": 1000,
        "skip_4k": True, "skip_hdr": True, "downscale_4k_to_1080": False,
        "allow_interlaced": False, "retain_originals_days": 7,
    },
    "tv": {
        "enabled": True,
        "cq_1080": 25, "cq_720": 26, "cq_sd": 25, "cq_4k_downscale": 24,
        "preset": "p5", "multipass": "qres", "output_10bit": True,
        "min_savings_pct": 10,
        "min_kbps_1080": 2500, "min_kbps_720": 1200, "min_kbps_sd": 700,
        "skip_4k": False, "skip_hdr": True, "downscale_4k_to_1080": True,
        "allow_interlaced": False, "retain_originals_days": 7,
    },
}

# Human-readable skip reasons (also the dry-run report's breakdown keys).
SKIP_REASONS = {
    "empty_file": "0-byte placeholder",
    "not_probed": "No codec metadata yet (run the probe backfill)",
    "already_hevc": "Already HEVC",
    "codec_excluded": "Codec not selected for conversion",
    "excluded_path": "In an excluded folder",
    "type_disabled": "Media type disabled",
    "hdr": "HDR / Dolby Vision",
    "4k": "4K (kept as-is)",
    "interlaced": "Interlaced",
    "unsupported_pix_fmt": "Unsupported pixel format",
    "low_bitrate": "Bitrate already low",
    "multi_video": "Multiple video streams",
}

INTERLACED_FIELD_ORDERS = {"tt", "bb", "tb", "bt"}
SUPPORTED_PIX_FMTS = {"yuv420p", "yuvj420p", "yuv420p10le"}
SDR = {None, "", "sdr"}


@dataclass
class Decision:
    eligible: bool
    reason: str = ""
    bucket: str = ""
    plan: Dict[str, Any] = field(default_factory=dict)


def resolution_bucket(height: Optional[int], width: Optional[int] = None) -> str:
    """'2160', '1080', '720' or 'sd' - width-aware so 1920x800 scope is 1080."""
    height, width = height or 0, width or 0
    if height >= 1600 or width >= 3200:
        return "2160"
    if height >= 900 or width >= 1800:
        return "1080"
    if height >= 600 or width >= 1200:
        return "720"
    return "sd"


def _hw_decodable(item: Dict[str, Any]) -> bool:
    # NVDEC handles 8-bit 4:2:0 h264 reliably; MPEG-4 ASP (packed B-frames,
    # GMC) and Hi10P go through software decode instead.
    return (item.get("video_codec") == "h264"
            and item.get("pix_fmt") in ("yuv420p", "yuvj420p")
            and (item.get("bit_depth") or 8) == 8)


def decide(item: Dict[str, Any], settings: Dict[str, Any], source_codecs: Iterable[str],
           excluded_prefixes: Iterable[str] = ()) -> Decision:
    """Eligibility + encode plan for one media_items row (as a dict)."""
    codec = (item.get("video_codec") or "").lower()
    bucket = resolution_bucket(item.get("height"), item.get("width"))

    def skip(reason: str) -> Decision:
        return Decision(False, reason, bucket)

    if not item.get("size_bytes"):
        return skip("empty_file")
    if item.get("probe_version") is None:
        return skip("not_probed")
    if codec == "hevc":
        return skip("already_hevc")
    if codec not in {c.lower() for c in source_codecs}:
        return skip("codec_excluded")
    path = item.get("path") or ""
    if any(prefix and path.startswith(prefix) for prefix in excluded_prefixes):
        return skip("excluded_path")
    if not settings.get("enabled", True):
        return skip("type_disabled")
    if item.get("hdr_format") not in SDR and settings.get("skip_hdr", True):
        return skip("hdr")

    scale_to_1080 = False
    if bucket == "2160":
        if settings.get("downscale_4k_to_1080") and not settings.get("skip_4k"):
            scale_to_1080 = True
        else:
            return skip("4k")
    if item.get("field_order") in INTERLACED_FIELD_ORDERS and not settings.get("allow_interlaced"):
        return skip("interlaced")
    if item.get("pix_fmt") not in SUPPORTED_PIX_FMTS:
        return skip("unsupported_pix_fmt")

    video_bps = item.get("video_bitrate") or item.get("bitrate") or 0
    threshold_bucket = "1080" if bucket == "2160" else bucket
    if video_bps < settings.get(f"min_kbps_{threshold_bucket}", 0) * 1000:
        return skip("low_bitrate")

    cq_key = "cq_4k_downscale" if scale_to_1080 else f"cq_{threshold_bucket}"  # gitleaks:allow (profile lookup key)
    plan = {
        "cq": settings[cq_key],
        "preset": settings.get("preset", "p6"),
        "multipass": settings.get("multipass", "qres"),
        "ten_bit": bool(settings.get("output_10bit", True)),
        "scale_width": 1920 if scale_to_1080 else None,
        "decode": "cuda" if _hw_decodable(item) else "sw",
        "deinterlace": item.get("field_order") in INTERLACED_FIELD_ORDERS,
        "max_video_bps": video_bps or None,
    }
    return Decision(True, "", "4k_down" if scale_to_1080 else bucket, plan)
