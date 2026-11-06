#!/bin/bash
# Recovers from "Failed to initialize NVML: Driver/library version mismatch":
# unattended-upgrades installed new NVIDIA userspace libs while the old kernel
# module stays loaded, so NVENC (transcode worker, Plex/Jellyfin pods) fails
# until the module matches. Stops future silent driver bumps, then reboots.
# Usage: sudo bash scripts/fix_nvidia_mismatch.sh [--no-reboot]
set -euo pipefail
cd "$(dirname "$0")/.."
LOG_DIR="tmp/runner/nvidia-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$LOG_DIR"
exec > >(tee "$LOG_DIR/log.txt") 2>&1

echo "loaded module: $(head -1 /proc/driver/nvidia/version 2>/dev/null)"
dpkg -l 'nvidia-utils-*' 2>/dev/null | awk '/^ii/{print "installed userspace: "$2" "$3}'

CONF=/etc/apt/apt.conf.d/51unattended-upgrades-nvidia
if [ ! -f "$CONF" ]; then
  cat > "$CONF" <<'APT'
// Driver upgrades need a reboot to load the matching kernel module; do them by hand.
Unattended-Upgrade::Package-Blacklist { "nvidia-"; "libnvidia-"; "xserver-xorg-video-nvidia-"; };
APT
  echo "wrote $CONF"
fi

if [ "${1:-}" = "--no-reboot" ]; then
  echo "skipping reboot; NVENC stays broken until the nvidia module is reloaded"
  exit 0
fi
echo "rebooting in 10 s (Ctrl-C to cancel)"
sleep 10
systemctl reboot
