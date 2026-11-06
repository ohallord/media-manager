"""Heuristic output-size estimate for the dry-run report (pure)."""
from statistics import median
from typing import Any, Dict, Iterable, Optional, Tuple

# Output video bitrate as a fraction of the source's, per (codec family,
# media_type). Starting points from typical NVENC HEVC results at the default
# CQs; calibrate() replaces them with real medians once jobs have run.
DEFAULT_RATIOS: Dict[Tuple[str, str], float] = {
    ("h264", "movie"): 0.55,
    ("h264", "tv"): 0.45,
    ("mpeg4", "movie"): 0.5,
    ("mpeg4", "tv"): 0.5,
}

# Ceiling on the estimated output video bitrate (bits/s): at these CQs HEVC
# rarely needs more, however bloated the source was.
VIDEO_CAPS_BPS: Dict[Tuple[str, str], int] = {
    ("movie", "1080"): 8_000_000, ("movie", "720"): 4_000_000, ("movie", "sd"): 2_000_000,
    ("tv", "1080"): 3_000_000, ("tv", "720"): 1_800_000, ("tv", "sd"): 1_000_000,
    ("tv", "4k_down"): 4_000_000, ("movie", "4k_down"): 10_000_000,
}

MIN_CALIBRATION_SAMPLES = 5


def codec_family(codec: Optional[str]) -> str:
    codec = (codec or "").lower()
    return "mpeg4" if codec.startswith("mpeg4") or codec.startswith("msmpeg4") else codec


def ratio_key(item: Dict[str, Any]) -> Tuple[str, str]:
    return codec_family(item.get("video_codec")), item.get("media_type") or "movie"


def estimate_output_bytes(item: Dict[str, Any], bucket: str,
                          ratios: Optional[Dict[Tuple[str, str], float]] = None) -> int:
    """Estimated size of the transcoded file (never more than the source)."""
    size = item.get("size_bytes") or 0
    duration = item.get("duration_seconds") or 0
    ratio = (ratios or DEFAULT_RATIOS).get(ratio_key(item), 0.55)
    total_bps = item.get("bitrate") or 0
    video_bps = item.get("video_bitrate") or 0
    if not duration or not total_bps:
        return int(size * ratio)
    if not video_bps or video_bps > total_bps:
        video_bps = int(total_bps * 0.85)  # unknown split: assume ~15% audio/subs
    other_bps = total_bps - video_bps
    cap = VIDEO_CAPS_BPS.get((item.get("media_type") or "movie", bucket))
    out_video_bps = video_bps * ratio
    if cap:
        out_video_bps = min(out_video_bps, cap)
    return int(min(size, (out_video_bps + other_bps) * duration / 8))


def calibrate(samples: Iterable[Tuple[Tuple[str, str], float]],
              base: Optional[Dict[Tuple[str, str], float]] = None) -> Dict[Tuple[str, str], float]:
    """
    samples: (ratio_key, actual output/source video ratio) from finished jobs.
    A key's ratio is replaced by the median once it has enough samples.
    """
    ratios = dict(base or DEFAULT_RATIOS)
    grouped: Dict[Tuple[str, str], list] = {}
    for key, value in samples:
        if value and value > 0:
            grouped.setdefault(key, []).append(value)
    for key, values in grouped.items():
        if len(values) >= MIN_CALIBRATION_SAMPLES:
            ratios[key] = round(median(values), 3)
    return ratios
