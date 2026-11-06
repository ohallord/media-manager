"""Builds ffmpeg command lines for the transcode worker (pure)."""
from typing import Any, Dict, List, Optional, Tuple

from media_manager.metadata import main_video_stream

# MP4 text subtitles can't be stored in Matroska as-is; SRT keeps the text.
TEXT_SUBS_NEEDING_SRT = {"mov_text", "tx3g"}

# mkvmerge's per-track statistics would keep reporting the *source* bitrate
# and size on the new video track (Plex shows them) - blank them out.
STALE_STAT_TAGS = (
    "BPS", "BPS-eng", "DURATION", "DURATION-eng", "NUMBER_OF_BYTES",
    "NUMBER_OF_BYTES-eng", "NUMBER_OF_FRAMES", "NUMBER_OF_FRAMES-eng",
    "_STATISTICS_TAGS", "_STATISTICS_TAGS-eng", "_STATISTICS_WRITING_APP",
    "_STATISTICS_WRITING_APP-eng", "_STATISTICS_WRITING_DATE_UTC",
    "_STATISTICS_WRITING_DATE_UTC-eng",
)


def _is_cover_art(stream: Dict[str, Any]) -> bool:
    return bool((stream.get("disposition") or {}).get("attached_pic"))


def stream_plan(probe: Dict[str, Any]) -> Dict[str, Any]:
    """
    Which input streams are kept (in output order) and which are dropped.
    Kept: the main video, every audio/subtitle/attachment stream. Dropped:
    cover art (posters come from the media server) and data/timecode tracks.
    """
    video = main_video_stream(probe)
    kept: List[Dict[str, Any]] = [video] if video else []
    dropped: List[str] = []
    for stream in probe.get("streams", []):
        kind = stream.get("codec_type")
        if stream is video:
            continue
        if kind in ("audio", "subtitle", "attachment"):
            kept.append(stream)
        else:
            label = "cover_art" if _is_cover_art(stream) else (kind or "unknown")
            dropped.append(f"{label}:{stream.get('codec_name') or '?'}#{stream.get('index')}")
    counts = {kind: sum(1 for s in kept if s.get("codec_type") == kind)
              for kind in ("audio", "subtitle", "attachment")}
    return {"video": video, "kept": kept, "counts": counts, "dropped": dropped}


def _video_filter(plan: Dict[str, Any], hw_frames: bool, encoder: str) -> str:
    ten_bit = plan.get("ten_bit", True)
    width = plan.get("scale_width")
    if hw_frames:
        steps = []
        if plan.get("deinterlace"):
            steps.append("bwdif_cuda=mode=send_frame")
        fmt = "p010le" if ten_bit else "nv12"
        steps.append(f"scale_cuda=w={width}:h=-2:interp_algo=lanczos:format={fmt}" if width
                     else f"scale_cuda=format={fmt}")
        return ",".join(steps)
    steps = []
    if plan.get("deinterlace"):
        steps.append("bwdif=mode=send_frame")
    if width:
        steps.append(f"scale={width}:-2:flags=lanczos")
    if ten_bit:
        steps.append("format=p010le" if encoder == "hevc_nvenc" else "format=yuv420p10le")
    else:
        steps.append("format=yuv420p")
    return ",".join(steps)


def _encoder_args(plan: Dict[str, Any], encoder: str) -> List[str]:
    profile = "main10" if plan.get("ten_bit", True) else "main"
    if encoder == "libx265":  # CPU fallback / CI end-to-end tests (no NVENC there)
        return ["-c:v", "libx265", "-preset", "medium", "-crf", str(plan["cq"]),
                "-profile:v", profile, "-x265-params", "log-level=error"]
    args = ["-c:v", "hevc_nvenc", "-preset", plan.get("preset", "p6"), "-tune", "hq",
            "-rc", "vbr", "-cq", str(plan["cq"]), "-b:v", "0"]
    max_bps = plan.get("max_video_bps")
    if max_bps:
        # Never let the encode exceed the source video's bitrate.
        args += ["-maxrate", str(int(max_bps)), "-bufsize", str(int(max_bps) * 2)]
    args += ["-multipass", plan.get("multipass", "qres"),
             "-spatial_aq", "1", "-temporal_aq", "1", "-aq-strength", "8",
             "-rc-lookahead", "32", "-bf", "4", "-b_ref_mode", "middle", "-g", "240",
             "-profile:v", profile]
    return args


def build_encode_args(probe: Dict[str, Any], plan: Dict[str, Any], src: str, dst: str,
                      encoder: str = "hevc_nvenc", ffmpeg: str = "ffmpeg",
                      start: Optional[float] = None, duration: Optional[float] = None,
                      force_sw_decode: bool = False) -> List[str]:
    """
    Full ffmpeg command: video re-encoded to HEVC, every audio/subtitle/
    attachment stream, chapter and tag copied untouched into Matroska.
    start/duration cut a sample (for the pre-encode savings check).
    """
    streams = stream_plan(probe)
    if not streams["video"]:
        raise ValueError("No video stream to transcode")
    hw_frames = plan.get("decode") == "cuda" and encoder == "hevc_nvenc" and not force_sw_decode
    container = (probe.get("format") or {}).get("format_name") or ""

    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-nostats", "-progress", "pipe:1"]
    if "avi" in container:
        args += ["-fflags", "+genpts"]  # AVI has no reliable timestamps
    if hw_frames:
        args += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", src]
    if duration is not None:
        args += ["-t", f"{duration:.3f}"]

    # A sample only measures video size; mapping sparse subtitle tracks (PGS)
    # would make ffmpeg read to the end of the file waiting for them.
    sample = start is not None or duration is not None
    mapped = streams["kept"][:1] if sample else streams["kept"]
    for stream in mapped:
        args += ["-map", f"0:{stream['index']}"]
    args += ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy"]

    sub_ordinal = 0
    for stream in mapped:
        if stream.get("codec_type") != "subtitle":
            continue
        if stream.get("codec_name") in TEXT_SUBS_NEEDING_SRT:
            args += [f"-c:s:{sub_ordinal}", "srt"]
        sub_ordinal += 1

    args += ["-vf", _video_filter(plan, hw_frames, encoder)]
    args += _encoder_args(plan, encoder)
    for tag in STALE_STAT_TAGS:
        args += ["-metadata:s:v:0", f"{tag}="]
    args += ["-fps_mode", "passthrough", "-max_muxing_queue_size", "4096", "-f", "matroska", dst]
    return args


def build_verify_decode_args(path: str, ffmpeg: str = "ffmpeg") -> List[str]:
    """Decodes the whole video track and discards it: any error = corrupt output."""
    return [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", path,
            "-map", "0:v:0", "-f", "null", "-"]


def parse_progress_line(line: str) -> Optional[Tuple[str, str]]:
    """'out_time_us=12345' -> ('out_time_us', '12345') from -progress output."""
    line = line.strip()
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    return key.strip(), value.strip()


def progress_fraction(out_time_us: Optional[str], duration_seconds: Optional[float]) -> Optional[float]:
    try:
        done = int(out_time_us) / 1_000_000
    except (TypeError, ValueError):
        return None
    if not duration_seconds or duration_seconds <= 0 or done < 0:
        return None
    return max(0.0, min(1.0, done / duration_seconds))
