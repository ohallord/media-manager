"""Checks a finished transcode before it may replace the original (pure)."""
from typing import Any, Dict, List, Optional, Tuple

from media_manager.metadata import main_video_stream
from media_manager.transcode.ffmpeg_args import TEXT_SUBS_NEEDING_SRT, stream_plan

PACKET_TOLERANCE = 0.001  # 0.1%


def _streams(probe: Dict[str, Any], kind: str) -> List[Dict[str, Any]]:
    return [s for s in probe.get("streams", []) if s.get("codec_type") == kind]


def _lang(stream: Dict[str, Any]) -> Optional[str]:
    # MP4 writes "und" (undetermined) where Matroska just omits the tag.
    lang = (stream.get("tags") or {}).get("language")
    return None if lang in (None, "", "und") else lang


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _duration(probe: Dict[str, Any]) -> float:
    try:
        return float((probe.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def compare(src_probe: Dict[str, Any], out_probe: Dict[str, Any], plan: Dict[str, Any],
            src_size: int, out_size: int, min_savings_pct: float,
            decode_errors: Optional[str] = None) -> Tuple[bool, List[str]]:
    """Returns (ok, failure codes). Any failure keeps the original file."""
    failures: List[str] = []
    expected = stream_plan(src_probe)

    src_dur, out_dur = _duration(src_probe), _duration(out_probe)
    if not out_dur or abs(src_dur - out_dur) > max(1.0, src_dur * 0.002):
        failures.append("duration_mismatch")

    for kind in ("audio", "subtitle", "attachment"):
        if len(_streams(out_probe, kind)) != expected["counts"][kind]:
            failures.append(f"{kind}_count_mismatch")

    src_audio = [s for s in expected["kept"] if s.get("codec_type") == "audio"]
    for src, out in zip(src_audio, _streams(out_probe, "audio")):
        if src.get("codec_name") != out.get("codec_name") or _lang(src) != _lang(out):
            failures.append("audio_stream_changed")
            break
    src_subs = [s for s in expected["kept"] if s.get("codec_type") == "subtitle"]
    for src, out in zip(src_subs, _streams(out_probe, "subtitle")):
        want = "subrip" if src.get("codec_name") in TEXT_SUBS_NEEDING_SRT else src.get("codec_name")
        if want != out.get("codec_name") or _lang(src) != _lang(out):
            failures.append("subtitle_stream_changed")
            break

    src_video, out_video = expected["video"], main_video_stream(out_probe)
    if not out_video or out_video.get("codec_name") != "hevc":
        failures.append("video_not_hevc")
    else:
        if plan.get("scale_width"):
            if out_video.get("width") != plan["scale_width"]:
                failures.append("video_size_mismatch")
        elif (out_video.get("width"), out_video.get("height")) != (src_video.get("width"), src_video.get("height")):
            failures.append("video_size_mismatch")
        if plan.get("ten_bit", True) and out_video.get("pix_fmt") != "yuv420p10le":
            failures.append("video_not_10bit")
        # Packet counts are only present when probed with -count_packets.
        src_pk, out_pk = _int(src_video.get("nb_read_packets")), _int(out_video.get("nb_read_packets"))
        if src_pk and (out_pk is None or abs(src_pk - out_pk) > src_pk * PACKET_TOLERANCE):
            failures.append("frame_count_mismatch")

    if decode_errors:
        failures.append("output_decode_errors")
    if not out_size or out_size > src_size * (1 - min_savings_pct / 100.0):
        failures.append("not_smaller")
    return (not failures), failures
