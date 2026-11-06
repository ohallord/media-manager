# NVIDIA driver/library mismatch (H.265 migration stalls)

**Symptom:** the migration makes no progress. The transcoder journal shows
`gpu_busy ... No capable devices found` every few seconds, or (after the fix in
this runbook's MR) `Waiting: GPU unavailable: Failed to initialize NVML:
Driver/library version mismatch`. Plex/Jellyfin hardware transcoding breaks too.

**Check:**
```bash
nvidia-smi                                   # "Driver/library version mismatch"
head -1 /proc/driver/nvidia/version          # loaded kernel module version
dpkg -l 'nvidia-utils-*' | awk '/^ii/'       # installed userspace version
grep -B2 -A3 nvidia /var/log/apt/history.log # who upgraded it (unattended-upgrade)
```

**Cause:** unattended-upgrades installed new NVIDIA userspace libraries; the
old kernel module stays loaded until a reboot.

**Alert:** admins get one email per outage ("GPU unavailable (reboot
needed?)"), and the H.265 Migration page shows the error on the worker line.

**Automatic fix:** `sudo bash scripts/install_gpu_reboot_timer.sh` installs a
root timer that, at 03:30 local time, reboots only if `nvidia-smi` still says
"Driver/library version mismatch". Check it: `systemctl list-timers
media-manager-gpu-reboot.timer`; last run: `journalctl -u media-manager-gpu-reboot`.

**Manual fix now:** `sudo bash scripts/fix_nvidia_mismatch.sh` (logs to `tmp/runner/`).
It blacklists NVIDIA packages from unattended-upgrades, then reboots. After the
reboot, `nvidia-smi` should work and the worker logs `Resuming: queue released`.
Upgrade drivers by hand afterwards and reboot in the same window.

First seen 2026-10-09 (595.91.07 module vs 595.99.02 libs, ~33 h stalled).
