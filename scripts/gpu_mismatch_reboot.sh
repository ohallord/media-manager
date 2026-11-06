#!/bin/bash
# Nightly (03:30 local) check run by media-manager-gpu-reboot.timer as root:
# reboots the host only when nvidia-smi reports a driver/library version
# mismatch (new NVIDIA userspace from unattended-upgrades, old kernel module
# still loaded), which leaves NVENC unusable until a reboot. Any other
# nvidia-smi failure is left alone - a reboot may not fix it.
# Installed to /usr/local/sbin by scripts/install_gpu_reboot_timer.sh.
set -uo pipefail
NVIDIA_SMI="${NVIDIA_SMI:-nvidia-smi}"
REBOOT_CMD="${REBOOT_CMD:-systemctl reboot}"

out="$("$NVIDIA_SMI" 2>&1)"
if grep -q "Driver/library version mismatch" <<<"$out"; then
    echo "NVIDIA driver/library version mismatch; rebooting to load the matching kernel module"
    $REBOOT_CMD
    exit $?
fi
echo "nvidia-smi OK or not a version mismatch; no reboot"
