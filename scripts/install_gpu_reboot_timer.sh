#!/bin/bash
# Installs media-manager-gpu-reboot.timer: at 03:30 local time, reboot the
# host only if the NVIDIA driver/library versions mismatch (see
# docs/runbooks/nvidia-driver-mismatch.md). The check script is copied to
# /usr/local/sbin (root-owned) so root never runs a user-writable file.
# Usage: sudo bash scripts/install_gpu_reboot_timer.sh [--uninstall]
set -euo pipefail
NAME=media-manager-gpu-reboot
BIN=/usr/local/sbin/${NAME}
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo bash $0" >&2
    exit 1
fi
if [ "${1:-}" = "--uninstall" ]; then
    systemctl disable --now "${NAME}.timer" 2>/dev/null || true
    rm -f "/etc/systemd/system/${NAME}.timer" "/etc/systemd/system/${NAME}.service" "$BIN"
    systemctl daemon-reload
    echo "removed ${NAME}"
    exit 0
fi

install -o root -g root -m 755 "$SRC_DIR/scripts/gpu_mismatch_reboot.sh" "$BIN"
cat > "/etc/systemd/system/${NAME}.service" <<UNIT
[Unit]
Description=Reboot if the NVIDIA driver/library versions mismatch (media-manager)

[Service]
Type=oneshot
ExecStart=${BIN}
UNIT
cat > "/etc/systemd/system/${NAME}.timer" <<UNIT
[Unit]
Description=Nightly NVIDIA mismatch check at 03:30 local time (media-manager)

[Timer]
OnCalendar=*-*-* 03:30:00
# A missed 03:30 (host off) must not turn into a daytime reboot.
Persistent=false

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now "${NAME}.timer"
systemctl list-timers "${NAME}.timer" --no-pager
