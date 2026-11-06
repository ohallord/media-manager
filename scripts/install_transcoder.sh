#!/bin/bash
# Installs media-manager-transcoder.service: the host H.265 (NVENC) worker.
#
# It runs from a dedicated checkout (default ~/media-manager-worker, kept in
# sync by the CI deploy job) - never from a dev checkout - using the system
# python3 (the worker is stdlib-only). Config/secrets come from an env file
# (mode 600) that must hold TRANSCODE_API_URL, TRANSCODE_WORKER_TOKEN and
# TRANSCODE_SCRATCH_DIR - see docs/INSTALL.md.
#
# Usage: sudo bash scripts/install_transcoder.sh [worker_dir] [env_file]
set -euo pipefail

RUN_USER="${SUDO_USER:-$(whoami)}"
RUN_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)
WORKER_DIR="${1:-$RUN_HOME/media-manager-worker}"
ENV_FILE="${2:-$RUN_HOME/.config/media-manager/transcoder.env}"
SERVICE_NAME="media-manager-transcoder"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo bash $0" >&2
    exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
    echo "Missing $ENV_FILE (TRANSCODE_API_URL, TRANSCODE_WORKER_TOKEN, TRANSCODE_SCRATCH_DIR)." >&2
    exit 1
fi
for key in TRANSCODE_API_URL TRANSCODE_WORKER_TOKEN TRANSCODE_SCRATCH_DIR; do
    grep -q "^${key}=." "$ENV_FILE" || { echo "$ENV_FILE has no ${key}" >&2; exit 1; }
done
chmod 600 "$ENV_FILE"
chown "$RUN_USER" "$ENV_FILE"

# First install: seed the worker checkout from this repo (CI keeps it current afterwards).
if [ ! -f "$WORKER_DIR/media_manager/transcode/worker.py" ]; then
    echo "Seeding $WORKER_DIR from $SRC_DIR..."
    mkdir -p "$WORKER_DIR"
    rsync -a --delete --exclude .git --exclude .venv --exclude tests --exclude 'media_manager/media.db*' \
        --exclude '*.log' "$SRC_DIR"/ "$WORKER_DIR"/
    chown -R "$RUN_USER":"$RUN_USER" "$WORKER_DIR"
fi

cat > "$SERVICE_FILE" <<UNIT
[Unit]
Description=Media Manager H.265 transcode worker (NVENC)
After=network-online.target remote-fs.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${WORKER_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/python3 -m media_manager.transcode.worker
# Exit 0 after a deploy (server version changed) restarts on the new code.
Restart=always
RestartSec=15
# Stay out of Plex's way: low CPU and idle-class disk priority.
Nice=10
IOSchedulingClass=idle
CPUWeight=50
# SIGTERM: an encode is stopped and requeued; a swap in progress is finished first.
KillSignal=SIGTERM
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 3
systemctl --no-pager --lines=15 status "$SERVICE_NAME" || true
echo
echo "Installed. Logs: journalctl -u $SERVICE_NAME -f"
echo "Nothing converts until an admin sets a mode and presses Start on the H.265 Migration page."
