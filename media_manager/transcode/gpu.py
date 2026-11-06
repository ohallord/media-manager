"""NVIDIA GPU load check: back off when Plex/Jellyfin/Emby need the encoder."""
import subprocess
from typing import Dict, Optional

QUERY = "encoder.stats.sessionCount,utilization.encoder,utilization.decoder,memory.free"
MIN_FREE_VRAM_MB = 1024
MAX_OTHER_ENCODER_UTIL = 20


def parse_query(output: str) -> Optional[Dict[str, int]]:
    """Parses one `nvidia-smi --query-gpu=... --format=csv,noheader,nounits` line."""
    line = (output or "").strip().splitlines()
    if not line:
        return None
    try:
        sessions, enc, dec, mem_free = [int(float(x.strip())) for x in line[0].split(",")]
    except ValueError:
        return None
    return {"encoder_sessions": sessions, "encoder_util": enc, "decoder_util": dec, "mem_free_mb": mem_free}


def query(nvidia_smi: str = "nvidia-smi", index: int = 0, run=subprocess.run) -> Optional[Dict[str, int]]:
    try:
        res = run([nvidia_smi, f"--id={index}", f"--query-gpu={QUERY}", "--format=csv,noheader,nounits"],
                  capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_query(res.stdout) if res.returncode == 0 else None


def unavailable(nvidia_smi: str = "nvidia-smi", index: int = 0, run=subprocess.run) -> Optional[str]:
    """
    Why the GPU can't take an encode at all (e.g. a driver/library version
    mismatch after an unattended driver upgrade), or None. ffmpeg reports that
    as "No capable devices found", which looks like contention, so without this
    the worker requeues the same job forever. A missing nvidia-smi is unknown,
    not broken: ffmpeg gets the final say.
    """
    try:
        res = run([nvidia_smi, f"--id={index}", f"--query-gpu={QUERY}", "--format=csv,noheader,nounits"],
                  capture_output=True, text=True, timeout=15)
    except OSError:
        return None
    except subprocess.TimeoutExpired:
        return "nvidia-smi timed out"
    if res.returncode == 0:
        return None
    lines = ((res.stdout or "") + "\n" + (res.stderr or "")).strip().splitlines()
    return lines[0].strip() if lines else f"nvidia-smi exited {res.returncode}"


def contention(info: Optional[Dict[str, int]], own_sessions: int) -> Optional[str]:
    """
    A reason to yield the GPU, or None. Only other processes' load counts:
    our own encode shows up as one session (even while paused) and most of
    the encoder. An idle session someone else holds open (e.g. a paused Plex
    stream) is not contention - only one that is actually encoding is.
    """
    if not info:
        return None
    if info["mem_free_mb"] < MIN_FREE_VRAM_MB:
        return f"low VRAM ({info['mem_free_mb']} MB free)"
    others = info["encoder_sessions"] - own_sessions
    if others > 0 and (own_sessions == 0 or info["encoder_util"] > MAX_OTHER_ENCODER_UTIL):
        return f"{others} other encode session(s) on the GPU"
    return None
