import json
import subprocess
import logging
import os
from fractions import Fraction
from typing import Any, Dict, Optional

# Bump when extract_details() starts producing new/corrected fields so the
# probe backfill (web/services/media_probe_backfill.py) re-probes old rows.
PROBE_VERSION = 1

# A hung CIFS read must not wedge a scan or the backfill forever.
FFPROBE_TIMEOUT_SECONDS = 120

# Columns on MediaItem filled from extract_details(); probed_at/probe_version
# are set alongside them by apply_probe_details().
DETAIL_FIELDS = (
    "video_profile", "pix_fmt", "bit_depth", "hdr_format", "dv_profile",
    "color_transfer", "color_primaries", "field_order", "frame_rate",
    "video_bitrate", "container", "duration_seconds",
    "audio_stream_count", "subtitle_stream_count", "attachment_count",
)


def run_ffprobe(file_path: str) -> Dict[str, Any]:
    """Runs ffprobe and returns its parsed JSON. Raises on failure or timeout."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", "json",
        file_path
    ]
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
        timeout=FFPROBE_TIMEOUT_SECONDS,
    )
    return json.loads(result.stdout)


def _is_cover_art(stream: Dict[str, Any]) -> bool:
    # MP4/M4V cover art shows up as an mjpeg/png "video" stream; it must never
    # be mistaken for the main video track.
    return bool((stream.get("disposition") or {}).get("attached_pic"))


def main_video_stream(probe: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video" and not _is_cover_art(stream):
            return stream
    return None


def _to_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _frame_rate(stream: Dict[str, Any]) -> Optional[float]:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if not raw or raw in ("0/0", "0"):
            continue
        try:
            fps = float(Fraction(raw))
        except (ValueError, ZeroDivisionError):
            continue
        if fps > 0:
            return round(fps, 3)
    return None


def _bit_depth(stream: Dict[str, Any]) -> Optional[int]:
    depth = _to_int(stream.get("bits_per_raw_sample"))
    if depth:
        return depth
    pix_fmt = stream.get("pix_fmt") or ""
    if not pix_fmt:
        return None
    for bits in (16, 14, 12, 10):
        # yuv420p10le style, plus semi-planar p010le / p016le
        if f"p{bits}" in pix_fmt or pix_fmt.startswith(f"p0{bits}"):
            return bits
    return 8


def _video_bitrate(stream: Dict[str, Any]) -> Optional[int]:
    rate = _to_int(stream.get("bit_rate"))
    if rate:
        return rate
    # MKV muxers (mkvmerge) store per-track stats as BPS / BPS-eng tags.
    for key, value in (stream.get("tags") or {}).items():
        if key.upper() == "BPS" or key.upper().startswith("BPS-"):
            rate = _to_int(value)
            if rate:
                return rate
    return None


def _hdr_format(stream: Dict[str, Any]):
    """Returns (hdr_format, dv_profile) from stream side data / color transfer."""
    for side in stream.get("side_data_list") or []:
        if "DOVI" in (side.get("side_data_type") or ""):
            return "dv", _to_int(side.get("dv_profile"))
    transfer = stream.get("color_transfer")
    if transfer == "smpte2084":
        return "hdr10", None
    if transfer == "arib-std-b67":
        return "hlg", None
    return "sdr", None


def extract_details(probe: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pure: maps raw ffprobe JSON to the rich codec columns in DETAIL_FIELDS.
    Video-specific fields are None when the file has no real video stream.
    """
    streams = probe.get("streams", [])
    fmt = probe.get("format", {}) or {}
    video = main_video_stream(probe)

    details: Dict[str, Any] = {field: None for field in DETAIL_FIELDS}
    details["container"] = (fmt.get("format_name") or None)
    details["duration_seconds"] = _to_float(fmt.get("duration"))
    details["audio_stream_count"] = sum(1 for s in streams if s.get("codec_type") == "audio")
    details["subtitle_stream_count"] = sum(1 for s in streams if s.get("codec_type") == "subtitle")
    details["attachment_count"] = sum(1 for s in streams if s.get("codec_type") == "attachment")

    if video:
        hdr_format, dv_profile = _hdr_format(video)
        details.update({
            "video_profile": video.get("profile"),
            "pix_fmt": video.get("pix_fmt"),
            "bit_depth": _bit_depth(video),
            "hdr_format": hdr_format,
            "dv_profile": dv_profile,
            "color_transfer": video.get("color_transfer"),
            "color_primaries": video.get("color_primaries"),
            "field_order": video.get("field_order"),
            "frame_rate": _frame_rate(video),
            "video_bitrate": _video_bitrate(video),
        })
    return details


def apply_probe_details(target, details: Optional[Dict[str, Any]]) -> None:
    """Copies extract_details() output onto a MediaItem (or any object)."""
    if not details:
        return
    from datetime import datetime
    for field in DETAIL_FIELDS:
        setattr(target, field, details.get(field))
    target.probed_at = datetime.utcnow()
    target.probe_version = PROBE_VERSION


def metadata_from_probe(data: Dict[str, Any], file_path: str) -> Dict[str, Any]:
    """Core metadata dict (the get_video_metadata shape) from ffprobe JSON."""
    ext = os.path.splitext(file_path)[1].lower()
    metadata = {
        "path": file_path,
        "format_name": data.get("format", {}).get("format_name"),
        "duration": float(data.get("format", {}).get("duration", 0) or 0),
        "size": int(data.get("format", {}).get("size", 0) or os.path.getsize(file_path)),
        "bit_rate": int(data.get("format", {}).get("bit_rate", 0) or 0),
        "video_codec": None,
        "width": 0,
        "height": 0,
        "audio_codec": None,
        "audio_channels": 0,
        "resolution_label": None,
        "details": extract_details(data),
    }

    for stream in data.get("streams", []):
        if _is_cover_art(stream):
            continue
        if stream.get("codec_type") == "video" and not metadata["video_codec"]:
            metadata["video_codec"] = stream.get("codec_name")
            metadata["width"] = int(stream.get("width", 0) or 0)
            metadata["height"] = int(stream.get("height", 0) or 0)
        elif stream.get("codec_type") == "audio" and not metadata["audio_codec"]:
            metadata["audio_codec"] = stream.get("codec_name")
            metadata["audio_channels"] = int(stream.get("channels", 0) or 0)

    # Determine resolution label
    if metadata["height"] >= 2160:
        metadata["resolution_label"] = "4K"
    elif metadata["height"] >= 1080:
        metadata["resolution_label"] = "1080p"
    elif metadata["height"] >= 720:
        metadata["resolution_label"] = "720p"
    elif metadata["height"] >= 480:
        metadata["resolution_label"] = "480p"
    elif metadata["height"] > 0:
        metadata["resolution_label"] = "SD"
    else:
        metadata["resolution_label"] = "Audio" if ext in [".mp3", ".ogg", ".flac", ".wav"] else "Unknown"
    return metadata


def get_video_metadata(file_path):
    """
    Extracts metadata from a video or audio file using ffprobe.
    """
    ext = os.path.splitext(file_path)[1].lower()
    try:
        # For ISO files, ffprobe might fail if it's not a mountable image
        # We'll still try but catch the error gracefully and return basic info
        return metadata_from_probe(run_ffprobe(file_path), file_path)
    except subprocess.CalledProcessError as e:
        # Specifically catch ffprobe failures to log stderr
        stderr_msg = e.stderr.strip() if e.stderr else "No stderr output"
        logging.warning(f"ffprobe failed for {file_path}: {stderr_msg}")
        return {
            "path": file_path,
            "format_name": ext.strip("."),
            "duration": 0,
            "size": os.path.getsize(file_path),
            "bit_rate": 0,
            "video_codec": "unknown",
            "width": 0,
            "height": 0,
            "audio_codec": "unknown",
            "audio_channels": 0,
            "resolution_label": "Unknown"
        }
    except Exception as e:
        logging.warning(f"Metadata extraction failed for {file_path}: {e}")
        return {
            "path": file_path,
            "format_name": ext.strip("."),
            "duration": 0,
            "size": os.path.getsize(file_path),
            "bit_rate": 0,
            "video_codec": "unknown",
            "width": 0,
            "height": 0,
            "audio_codec": "unknown",
            "audio_channels": 0,
            "resolution_label": "Unknown"
        }
