"""Version handshake between the web app and the host transcode worker."""

# Bump on any incompatible change to the /api/v1/transcode/worker/* payloads.
# The server accepts the current and previous version so a worker can finish
# its in-flight job across a deploy before restarting on the new code.
PROTOCOL_VERSION = 1


def is_compatible(worker_version) -> bool:
    try:
        version = int(worker_version)
    except (TypeError, ValueError):
        return False
    return version in (PROTOCOL_VERSION, PROTOCOL_VERSION - 1) and version >= 1
