"""Filesystem helpers for the worker's copy / install / swap steps."""
import hashlib
import os
import shutil
from typing import Iterable, List, Optional, Tuple

CHUNK = 8 * 1024 * 1024
NETWORK_FS = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs"}
PARTIAL_SUFFIX = ".mm-partial"
BACKUP_SUFFIX = ".mm-orig"


def volume_ok(path: Optional[str]) -> bool:
    """
    Same rule as quality.volume_is_healthy (a mount point that is an empty or
    missing directory means the mount dropped) - duplicated so the worker
    never imports the app's DB-backed modules.
    """
    try:
        return bool(path) and os.path.isdir(path) and len(os.listdir(path)) > 0
    except OSError:
        return False


def storage_root(path: str, media_dirs: Iterable[str]) -> str:
    """The library root a file lives under (longest matching prefix), else its folder."""
    best = ""
    for root in media_dirs:
        root = root.rstrip("/")
        if root and (path == root or path.startswith(root + "/")) and len(root) > len(best):
            best = root
    return best or os.path.dirname(path)


def is_inside(path: str, roots: Iterable[str]) -> bool:
    path = os.path.realpath(path).rstrip("/")
    for root in roots:
        root = os.path.realpath(root).rstrip("/") if root else ""
        if root and (path == root or path.startswith(root + "/")):
            return True
    return False


def filesystem_type(path: str, mounts_file: str = "/proc/mounts") -> str:
    """fstype of the mount holding path ('' if unknown)."""
    path = os.path.realpath(path)
    best, fstype = "", ""
    try:
        with open(mounts_file) as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount = parts[1].replace("\\040", " ")
                if (path == mount or path.startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best):
                    best, fstype = mount, parts[2]
    except OSError:
        pass
    return fstype


def is_network_path(path: str, mounts_file: str = "/proc/mounts") -> bool:
    return filesystem_type(path, mounts_file) in NETWORK_FS


def same_filesystem(a: str, b: str) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def free_bytes(path: str) -> int:
    return shutil.disk_usage(path).free


def output_path_for(source: str) -> str:
    return os.path.splitext(source)[0] + ".mkv"


def temp_path_for(output: str, job_id: str) -> str:
    """Hidden, non-video extension: scans, Plex and Jellyfin ignore it."""
    folder, name = os.path.split(output)
    return os.path.join(folder, f".{os.path.splitext(name)[0]}.{job_id[:8]}{PARTIAL_SUFFIX}")


def backup_path_for(source: str) -> str:
    """
    Hidden like the temp file: a FileBot `-rename -r` over the library treats
    a visible "Movie.mkv.mm-orig" as a sidecar of "Movie.mkv" and renames it
    to "Movie.mm-orig", leaving the job's backup_path pointing at nothing.
    """
    folder, name = os.path.split(source)
    return os.path.join(folder, f".{name}{BACKUP_SUFFIX}")


def backup_candidates(backup: str) -> List[str]:
    """
    Where a kept original may be now: the recorded path, plus the name FileBot
    gives a pre-hidden "Movie.mkv.mm-orig" ("Movie.mm-orig").
    """
    folder, name = os.path.split(backup)
    found = [backup]
    if not name.startswith(".") and name.endswith(BACKUP_SUFFIX):
        stem = os.path.splitext(name[:-len(BACKUP_SUFFIX)])[0]
        found.append(os.path.join(folder, stem + BACKUP_SUFFIX))
    return found


def find_backup(backup: Optional[str]) -> Optional[str]:
    """The first existing location of a kept original, or None."""
    for path in backup_candidates(backup) if backup else []:
        if os.path.exists(path):
            return path
    return None


def fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # some network filesystems don't support directory fsync
    finally:
        os.close(fd)


def hash_file(path: str) -> str:
    digest = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_with_hash(src: str, dst: str) -> Tuple[int, str]:
    """Streams src to dst, fsyncs it, and returns (bytes, blake2b of what was written)."""
    digest = hashlib.blake2b(digest_size=32)
    size = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for chunk in iter(lambda: fin.read(CHUNK), b""):
            fout.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    fsync_dir(os.path.dirname(dst) or ".")
    return size, digest.hexdigest()


def apply_ownership(path: str, uid: int, gid: int, mode: int) -> None:
    """Match the mover's ownership rules; failures (e.g. on CIFS) are harmless."""
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def can_write_dir(path: str) -> bool:
    return os.access(path, os.W_OK | os.X_OK)


def remove_quietly(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
