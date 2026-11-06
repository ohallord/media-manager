#!/bin/bash
# Re-owns and re-permissions the media library so everything belongs to the
# media user (default UID/GID 1000) instead of root.
#
# Why: the web pod runs as root, so folders/files it creates (.nfo metadata,
# new season folders) end up root-owned. The host transcode worker and the
# watcher run as the media user and then can't write there (jobs get skipped as
# permission_denied).
#
# What it does, per library folder (local disks only - CIFS/NFS shares take
# their ownership from the mount options and are skipped):
#   - chown to OWNER (default 1000:1000) anything owned by someone else
#   - add missing permission bits: folders >= 775, files >= 664
#     (bits are only added, never removed - executables stay executable)
#
# Dry run by default; pass --apply to change anything.
#
# Usage:
#   bash scripts/fix_permissions.sh                  # dry run, all library folders
#   sudo bash scripts/fix_permissions.sh --apply     # fix them
#   sudo bash scripts/fix_permissions.sh --apply /media/TvShows "/media/Movies/Some Movie"
#   options: --owner UID:GID   (default 1000:1000, i.e. config.UID/GID)
set -euo pipefail

APPLY=0
OWNER="${MEDIA_OWNER:-1000:1000}"
DIRS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --owner) shift; OWNER="${1:?--owner needs UID:GID}" ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; exit 2 ;;
        *) DIRS+=("$1") ;;
    esac
    shift
done

if ! [[ "$OWNER" =~ ^[0-9]+:[0-9]+$ ]]; then
    echo "--owner must be numeric UID:GID (got '$OWNER')" >&2
    exit 2
fi
OWNER_UID="${OWNER%%:*}"
OWNER_GID="${OWNER##*:}"

if [ ${#DIRS[@]} -eq 0 ]; then
    # Default: the app's library folders (MEDIA_DIRECTORIES from config / env).
    REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
    mapfile -t DIRS < <(cd "$REPO_DIR" && python3 -c \
        'from media_manager.config import MEDIA_DIRECTORIES; print("\n".join(MEDIA_DIRECTORIES))')
fi

if [ "$APPLY" -eq 1 ] && [ "$(id -u)" -ne 0 ] && [ "$(id -u)" -ne "$OWNER_UID" ]; then
    echo "--apply needs root (sudo) to change ownership." >&2
    exit 1
fi

NETWORK_FS_RE='^(cifs|smb3|smbfs|nfs|nfs4|fuse\.sshfs)$'
total_owner=0
total_mode=0

for dir in "${DIRS[@]}"; do
    dir="${dir%/}"
    if [ ! -d "$dir" ]; then
        echo "skip  $dir (not a directory / not mounted)"
        continue
    fi
    fstype="$(findmnt -no FSTYPE -T "$dir" 2>/dev/null || true)"
    if [[ "$fstype" =~ $NETWORK_FS_RE ]]; then
        echo "skip  $dir ($fstype share - ownership comes from the mount options)"
        continue
    fi
    if [ -z "$(ls -A "$dir" 2>/dev/null)" ]; then
        echo "skip  $dir (empty - mount may be down)"
        continue
    fi

    wrong_owner=$(find "$dir" \( ! -uid "$OWNER_UID" -o ! -gid "$OWNER_GID" \) -print 2>/dev/null | wc -l)
    wrong_mode=$(find "$dir" \( \( -type d ! -perm -775 \) -o \( -type f ! -perm -664 \) \) -print 2>/dev/null | wc -l)
    total_owner=$((total_owner + wrong_owner))
    total_mode=$((total_mode + wrong_mode))
    printf '%-6s %s: %d with wrong owner, %d missing group/other permissions\n' \
        "$([ "$APPLY" -eq 1 ] && echo fix || echo check)" "$dir" "$wrong_owner" "$wrong_mode"

    if [ "$APPLY" -eq 1 ]; then
        find "$dir" \( ! -uid "$OWNER_UID" -o ! -gid "$OWNER_GID" \) -exec chown -h "$OWNER" {} + 2>/dev/null || true
        find "$dir" -type d ! -perm -775 -exec chmod u+rwx,g+rwx,o+rx {} + 2>/dev/null || true
        find "$dir" -type f ! -perm -664 -exec chmod u+rw,g+rw,o+r {} + 2>/dev/null || true
    fi
done

echo
if [ "$APPLY" -eq 1 ]; then
    echo "Done: fixed ownership on $total_owner and permissions on $total_mode entries (owner $OWNER)."
else
    echo "Dry run: $total_owner entries need a new owner and $total_mode need permission bits."
    [ $((total_owner + total_mode)) -gt 0 ] && echo "Re-run with: sudo bash $0 --apply"
fi
exit 0
