"""Transcode failure codes and what the queue does with each (pure)."""

# Worth one retry: the environment, not the file, was the problem.
TRANSIENT = {
    "volume_unhealthy",      # mount dropped / NAS unreachable
    "copy_failed",           # NAS -> scratch or scratch -> library copy broke
    "encoder_crash",         # ffmpeg died without a clear verdict on the file
    "disk_full",             # no space on scratch or target volume
    "source_changed",        # file replaced/edited since it was queued
    "hash_mismatch",         # installed copy didn't match what was encoded
    "worker_lost",           # lease expired mid-encode (crash, reboot)
}

# Requeued without spending an attempt: nothing was wrong with the job.
FREE_REQUEUE = {
    "gpu_busy",              # NVENC sessions/VRAM taken by Plex/Jellyfin/Emby
    "worker_shutdown",       # SIGTERM / redeploy drain
    "paused_too_long",       # streams kept the job paused past the limit
}

# The file itself won't convert usefully - skip it and keep the original.
PERMANENT = {
    "not_smaller",
    "predicted_not_smaller",
    "verify_failed",
    "source_corrupt",
    "source_missing",
    "permission_denied",
    "not_eligible",
    "unsupported_stream",
    "needs_manual_review",   # recovery found files it couldn't explain; nothing touched
}

MAX_ATTEMPTS = 2  # the first try + one retry


def classify(code: str) -> str:
    """'retry', 'requeue' (free) or 'skip'. Unknown codes are treated as transient."""
    if code in FREE_REQUEUE:
        return "requeue"
    if code in PERMANENT:
        return "skip"
    return "retry"
