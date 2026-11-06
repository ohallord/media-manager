"""
Host H.265 transcode worker (media-manager-transcoder.service).

Claims one job at a time from the web app, encodes on the local NVIDIA GPU,
verifies the result, and swaps it into the library with a write-ahead
protocol: every irreversible step is announced to the server (and
acknowledged) before it happens, so a crash at any point can be finished or
rolled back on restart (see transcode/recovery.py). It never touches the DB.

Run:  python3 -m media_manager.transcode.worker   (config from env, see WorkerConfig)
"""
import errno
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from media_manager.transcode import fsops, gpu, recovery, runner
from media_manager.transcode.client import ApiError, WorkerClient
from media_manager.transcode.ffmpeg_args import build_encode_args, build_verify_decode_args, progress_fraction, stream_plan
from media_manager.transcode.verify import compare

logger = logging.getLogger("media_manager.transcode.worker")

GPU_BUSY_MARKERS = ("OpenEncodeSessionEx failed", "out of memory", "No capable devices found",
                    "CUDA_ERROR_OUT_OF_MEMORY", "incompatible client key")
SAMPLE_SECONDS = 20
SAMPLE_POSITIONS = (0.2, 0.5, 0.8)
SAMPLE_MIN_DURATION = 180
SAMPLE_SAFETY_FACTOR = 1.5  # predicted savings must clear min_savings_pct * this
RESUME_AFTER_CLEAR_SECONDS = 120
SWAP_RETRY_SECONDS = 60
MUXER_NOISE_PREFIXES = ("[null @", "[out#")
GB = 1024 ** 3


class JobFailed(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class LeaseKeeper:
    """
    Keeps a job's lease alive from a background thread. The server expires a
    lease after 180 s without contact, but the network copy, ffprobe, sample
    encodes, the verify decode and the install copy of a big file each run
    much longer than that without saying anything - so the sweeper used to
    requeue (or flip to needs_recovery) jobs the worker was still busy with.
    Renews only when the main thread has been quiet for `interval` seconds,
    so it never overwrites the encode loop's fps/eta.
    """

    def __init__(self, client: WorkerClient, job_id: str, interval: float, check_interval: float,
                 clock: Callable[[], float] = time.monotonic):
        self.client = client
        self.job_id = job_id
        self.interval = interval
        self.check_interval = check_interval
        self.clock = clock
        self.renewed_at = clock()
        self.lost: Optional[JobFailed] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"lease-{job_id[:8]}", daemon=True)

    def start(self) -> "LeaseKeeper":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def touch(self) -> None:
        self.renewed_at = self.clock()

    def _run(self) -> None:
        while not self._stop.wait(self.check_interval):
            if self.clock() - self.renewed_at >= self.interval:
                self.renew()

    def renew(self) -> None:
        try:
            resp = self.client.progress(self.job_id)
        except ApiError as e:
            if e.status == 409:  # the server gave the job away; nothing we finish now will be accepted
                self.lost = JobFailed("worker_lost", f"server rejected lease renewal: {e.detail}")
                self._stop.set()
                return
            logger.warning(f"[TRANSCODE_WORKER] Lease renewal failed: {e}")
            return
        except Exception as e:  # network trouble: keep trying until the lease runs out
            logger.warning(f"[TRANSCODE_WORKER] Lease renewal failed: {e}")
            return
        self.touch()
        if resp.get("cancel"):
            self.lost = JobFailed("worker_shutdown", "cancelled by admin")
            self._stop.set()


@dataclass
class WorkerConfig:
    api_url: str
    token: str
    scratch_dir: str
    worker_id: str
    version: str
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    nvidia_smi: str = "nvidia-smi"
    gpu_index: int = 0
    # Installed by scripts/install_gpu_reboot_timer.sh; reported so alerts can say whether the host self-heals.
    gpu_reboot_timer: str = "/etc/systemd/system/media-manager-gpu-reboot.timer"
    encoder: str = "hevc_nvenc"
    tls_verify: bool = True
    media_dirs: List[str] = field(default_factory=list)
    source_dirs: List[str] = field(default_factory=list)
    uid: int = 1000
    gid: int = 1000
    file_mode: int = 0o664
    idle_sleep: float = 60
    paused_sleep: float = 30
    poll_interval: float = 1
    progress_interval: float = 15
    gpu_check_interval: float = 30
    max_pause_seconds: float = 3600
    lease_renew_interval: float = 45  # server lease is 180 s
    lease_check_interval: float = 5
    sample_check: bool = True

    @classmethod
    def from_env(cls, env=os.environ) -> "WorkerConfig":
        from media_manager import config as app_config  # env-only module, no DB
        missing = [k for k in ("TRANSCODE_API_URL", "TRANSCODE_WORKER_TOKEN", "TRANSCODE_SCRATCH_DIR") if not env.get(k)]
        if missing:
            raise SystemExit(f"Missing required environment: {', '.join(missing)}")
        return cls(
            api_url=env["TRANSCODE_API_URL"], token=env["TRANSCODE_WORKER_TOKEN"],
            scratch_dir=env["TRANSCODE_SCRATCH_DIR"],
            worker_id=env.get("TRANSCODE_WORKER_ID") or socket.gethostname(),
            version=app_config.VERSION,
            ffmpeg=env.get("FFMPEG_BIN", "ffmpeg"), ffprobe=env.get("FFPROBE_BIN", "ffprobe"),
            gpu_index=int(env.get("TRANSCODE_GPU_INDEX", "0")),
            encoder=env.get("TRANSCODE_ENCODER", "hevc_nvenc"),
            tls_verify=env.get("TRANSCODE_TLS_VERIFY", "1") not in ("0", "false", "no"),
            media_dirs=list(app_config.MEDIA_DIRECTORIES),
            source_dirs=[d["from_dir"] for d in app_config.SOURCE_DIRECTORIES if d.get("from_dir")],
            uid=app_config.UID, gid=app_config.GID, file_mode=app_config.PERMISSIONS,
            sample_check=env.get("TRANSCODE_SAMPLE_CHECK", "1") not in ("0", "false", "no"),
        )


class Worker:
    def __init__(self, cfg: WorkerConfig, client: WorkerClient, popen=subprocess.Popen, run=subprocess.run,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 gpu_query: Optional[Callable[[], Optional[Dict[str, int]]]] = None,
                 gpu_health: Optional[Callable[[], Optional[str]]] = None,
                 network_check: Callable[[str], bool] = fsops.is_network_path):
        self.cfg = cfg
        self.client = client
        self.popen = popen
        self.run = run
        self.sleep = sleep
        self.clock = clock
        self.gpu_query = gpu_query or (lambda: gpu.query(cfg.nvidia_smi, cfg.gpu_index, run=run))
        self.gpu_health = gpu_health or (lambda: gpu.unavailable(cfg.nvidia_smi, cfg.gpu_index, run=run))
        self.is_network = network_check
        self.stop_requested = False
        self.settings_by_type: Dict[str, Dict[str, Any]] = {}
        self._last_pause_reason: Optional[str] = None
        self._gpu_error: Optional[str] = None
        self._lease: Optional[LeaseKeeper] = None

    # ---------- lifecycle ----------

    def request_stop(self, *_):
        logger.info("[TRANSCODE_WORKER] Stop requested - finishing up.")
        self.stop_requested = True

    def check_environment(self) -> None:
        os.makedirs(self.cfg.scratch_dir, exist_ok=True)
        if not fsops.can_write_dir(self.cfg.scratch_dir):
            raise SystemExit(f"Scratch dir {self.cfg.scratch_dir} is not writable")
        # A scratch dir inside a library or download folder would be scanned/moved.
        if fsops.is_inside(self.cfg.scratch_dir, self.cfg.media_dirs + self.cfg.source_dirs):
            raise SystemExit(f"Scratch dir {self.cfg.scratch_dir} must not be inside a media or source directory")

    def run_forever(self, once: bool = False) -> str:
        self.check_environment()
        self._heartbeat()
        self.recover_inflight()
        while not self.stop_requested:
            self._gpu_error = self.gpu_health() if self.cfg.encoder == "hevc_nvenc" else None
            hb = self._heartbeat()
            if self._should_drain():
                logger.info(f"[TRANSCODE_WORKER] Server is v{self.client.server_version}, worker is "
                            f"v{self.cfg.version}: exiting so systemd restarts on the new code.")
                return "drain"
            if hb.get("pause"):
                reason = hb.get("reason") or "paused"
                if reason != self._last_pause_reason:  # log changes, not every 30 s
                    logger.info(f"[TRANSCODE_WORKER] Waiting: {reason}")
                    self._last_pause_reason = reason
                if once:
                    return "paused"
                self.sleep(self.cfg.paused_sleep)
                continue
            if self._gpu_error:
                reason = f"GPU unavailable: {self._gpu_error}"
                if reason != self._last_pause_reason:
                    logger.warning(f"[TRANSCODE_WORKER] Waiting: {reason} (a driver upgrade needs a reboot?)")
                    self._last_pause_reason = reason
                if once:
                    return "gpu_unavailable"
                self.sleep(self.cfg.paused_sleep)
                continue
            if self._last_pause_reason is not None:
                logger.info("[TRANSCODE_WORKER] Resuming: queue released")
                self._last_pause_reason = None
            claimed = self.client.claim()
            if not claimed:
                if once:
                    return "idle"
                self.sleep(self.cfg.idle_sleep)
                continue
            self.settings_by_type[claimed["job"].get("media_type") or "movie"] = claimed.get("settings") or {}
            self.process(claimed["job"], claimed.get("settings") or {})
            if once:
                return "done"
        return "stopped"

    def _heartbeat(self) -> Dict[str, Any]:
        hb = self.client.heartbeat({"pid": os.getpid(), "encoder": self.cfg.encoder, "gpu_error": self._gpu_error,
                                    "auto_reboot": os.path.exists(self.cfg.gpu_reboot_timer)})
        self.settings_by_type.update(hb.get("settings") or {})
        return hb

    def _should_drain(self) -> bool:
        return bool(self.client.server_version) and self.client.server_version != self.cfg.version

    # ---------- one job ----------

    def _keep_lease(self, job_id: str) -> LeaseKeeper:
        self._lease = LeaseKeeper(self.client, job_id, self.cfg.lease_renew_interval,
                                  self.cfg.lease_check_interval, clock=self.clock).start()
        return self._lease

    def _release_lease(self) -> None:
        if self._lease is not None:
            self._lease.stop()
            self._lease = None

    def _phase(self, job_id: str, phase: str, **paths) -> None:
        """Write-ahead phase report. A lost lease means someone else owns the job now: stop cleanly."""
        if self._lease is not None and self._lease.lost is not None:
            raise self._lease.lost
        try:
            self.client.phase(job_id, phase, **paths)
        except ApiError as e:
            if e.status == 409:
                raise JobFailed("worker_lost", f"server rejected phase {phase}: {e.detail}")
            raise
        if self._lease is not None:
            self._lease.touch()

    def process(self, job: Dict[str, Any], settings: Dict[str, Any]) -> None:
        scratch: List[str] = []
        self._keep_lease(job["id"])
        try:
            self._process(job, settings, scratch)
        except JobFailed as e:
            logger.warning(f"[TRANSCODE_WORKER] {job.get('title')}: {e.code} {e.message}")
            self._report_failure(job["id"], e.code, e.message)
        except Exception as e:  # unexpected: never leave the job hanging
            logger.exception(f"[TRANSCODE_WORKER] {job.get('title')}: unexpected error")
            self._report_failure(job["id"], "encoder_crash", repr(e))
        finally:
            self._release_lease()
            for path in scratch:
                fsops.remove_quietly(path)

    def _report_failure(self, job_id: str, code: str, message: str) -> None:
        try:
            self.client.fail(job_id, code, message)
        except Exception as e:
            logger.error(f"[TRANSCODE_WORKER] Could not report failure for {job_id}: {e}")

    def _process(self, job: Dict[str, Any], settings: Dict[str, Any], scratch: List[str]) -> None:
        job_id, src, plan = job["id"], job["source_path"], job.get("plan") or {}
        output = fsops.output_path_for(src)
        temp = fsops.temp_path_for(output, job_id)
        backup = fsops.backup_path_for(src)
        root = fsops.storage_root(src, self.cfg.media_dirs)

        self._preflight(job, src, root, output, need_target=job["source_size"])
        self._phase(job_id, "preparing")

        work_in = src
        if self.is_network(src):
            # Encoding straight off CIFS stalls on every mount hiccup: work from a local copy.
            work_in = os.path.join(self.cfg.scratch_dir, f"{job_id}.src{os.path.splitext(src)[1]}")
            scratch.append(work_in)
            self._require_space(self.cfg.scratch_dir, job["source_size"] * 2 + 5 * GB, "scratch")
            try:
                fsops.copy_with_hash(src, work_in)
            except OSError as e:
                raise JobFailed("disk_full" if e.errno == errno.ENOSPC else "copy_failed", str(e))
        else:
            self._require_space(self.cfg.scratch_dir, job["source_size"] + 5 * GB, "scratch")

        try:
            src_probe = runner.probe(work_in, self.cfg.ffprobe, count_packets=True, run=self.run)
        except Exception as e:
            if self.stop_requested:  # SIGTERM hits ffprobe too
                raise JobFailed("worker_shutdown", f"ffprobe interrupted: {e}")
            raise JobFailed("source_corrupt", f"ffprobe failed: {e}")
        if not stream_plan(src_probe)["video"]:
            raise JobFailed("unsupported_stream", "no video stream")

        if self.cfg.sample_check:
            self._sample_check(job, settings, src_probe, plan, work_in, scratch)

        scratch_out = os.path.join(self.cfg.scratch_dir, f"{job_id}.mkv")
        scratch.append(scratch_out)
        self._phase(job_id, "encoding")
        self._encode(job, src_probe, plan, work_in, scratch_out)

        self._phase(job_id, "verifying")
        out_size = os.path.getsize(scratch_out)
        try:
            out_probe = runner.probe(scratch_out, self.cfg.ffprobe, count_packets=True, run=self.run)
        except Exception as e:
            raise JobFailed("verify_failed", f"cannot probe output: {e}")
        decode_errors = self._decode_errors(scratch_out)
        ok, failures = compare(src_probe, out_probe, plan, job["source_size"], out_size,
                               settings.get("min_savings_pct", 10), decode_errors)
        if not ok:
            code = "not_smaller" if failures == ["not_smaller"] else "verify_failed"
            pct = 100 * (1 - out_size / job["source_size"]) if job["source_size"] else 0
            raise JobFailed(code, f"{','.join(failures)} (output {pct:.0f}% smaller)")

        # ---- install the verified file next to the original ----
        self._preflight(job, src, root, output, need_target=out_size + GB)
        self._phase(job_id, "installing", temp_path=temp, output_path=output, backup_path=backup)
        scratch.append(temp)  # removed on any failure; a no-op once it has been renamed into place
        try:
            _, digest = fsops.copy_with_hash(scratch_out, temp)
        except OSError as e:
            fsops.remove_quietly(temp)
            raise JobFailed("disk_full" if e.errno == errno.ENOSPC else "copy_failed", str(e))
        fsops.apply_ownership(temp, self.cfg.uid, self.cfg.gid, self.cfg.file_mode)
        if fsops.hash_file(temp) != digest:  # re-read: catches silent NAS write corruption
            fsops.remove_quietly(temp)
            raise JobFailed("hash_mismatch", "installed copy differs from the encode")
        self._phase(job_id, "installing", output_hash=digest)

        # ---- the swap: announced first, then never interrupted ----
        try:
            self._preflight(job, src, root, output, need_target=0)
        except JobFailed:
            fsops.remove_quietly(temp)
            raise
        self._announce_swap(job_id)
        self._swap(src, backup, temp, output)
        self._phase(job_id, "swapped")
        self._finish(job, output, digest, backup, settings,
                     {"failures": [], "dropped_streams": stream_plan(src_probe)["dropped"],
                      "source_size": job["source_size"], "output_size": out_size})

    def _announce_swap(self, job_id: str) -> None:
        """The server answers 423 while someone is watching the file: wait (keeping the lease) and retry."""
        waited = 0.0
        while True:
            try:
                self._phase(job_id, "swapping")
                return
            except ApiError as e:
                if e.status != 423:
                    raise
            if waited == 0:
                logger.info("[TRANSCODE_WORKER] File is being played; waiting to swap it in")
            if waited >= self.cfg.max_pause_seconds or self.stop_requested:
                raise JobFailed("paused_too_long", "file stayed in use; will retry later")
            self.client.progress(job_id)  # keep the lease alive while waiting
            self.sleep(SWAP_RETRY_SECONDS)
            waited += SWAP_RETRY_SECONDS

    def _preflight(self, job: Dict[str, Any], src: str, root: str, output: str, need_target: int) -> None:
        if not fsops.volume_ok(root):
            raise JobFailed("volume_unhealthy", f"{root} looks unmounted")
        try:
            st = os.stat(src)
        except FileNotFoundError:
            raise JobFailed("source_missing", src)
        if st.st_size != job["source_size"] or (job.get("source_mtime") and abs(st.st_mtime - job["source_mtime"]) > 2):
            raise JobFailed("source_changed", "file was modified since it was queued")
        folder = os.path.dirname(src)
        if not fsops.can_write_dir(folder):
            raise JobFailed("permission_denied", f"cannot write to {folder}")
        if output != src and os.path.exists(output):
            raise JobFailed("not_eligible", f"{output} already exists")
        if need_target:
            self._require_space(folder, need_target, "library volume")

    def _require_space(self, path: str, needed: int, label: str) -> None:
        try:
            free = fsops.free_bytes(path)
        except OSError as e:
            raise JobFailed("volume_unhealthy", str(e))
        if free < needed:
            raise JobFailed("disk_full", f"{label} has {free / GB:.1f} GB free, needs {needed / GB:.1f} GB")

    def _swap(self, src: str, backup: str, temp: str, output: str) -> None:
        folder = os.path.dirname(src)
        try:
            os.rename(src, backup)
        except OSError as e:
            fsops.remove_quietly(temp)
            raise JobFailed("permission_denied" if e.errno in (errno.EACCES, errno.EPERM) else "copy_failed", str(e))
        try:
            os.rename(temp, output)
        except OSError as e:
            os.rename(backup, src)  # put the original back before reporting
            fsops.remove_quietly(temp)
            raise JobFailed("copy_failed", f"could not move new file into place: {e}")
        fsops.fsync_dir(folder)

    def _finish(self, job: Dict[str, Any], output: str, digest: Optional[str], backup: str,
                settings: Dict[str, Any], verify_info: Dict[str, Any]) -> None:
        try:
            final_probe = runner.probe(output, self.cfg.ffprobe, run=self.run)
        except Exception:
            final_probe = None
        backup_kept: Optional[str] = backup
        if not settings.get("retain_originals_days", 0) and os.path.exists(backup):
            fsops.remove_quietly(backup)
            backup_kept = None
        self.client.complete(job["id"], {
            "output_path": output, "output_size": os.path.getsize(output), "output_hash": digest,
            "backup_path": backup_kept if backup_kept and os.path.exists(backup_kept) else None,
            "probe": final_probe, "verify": verify_info,
        })
        saved = job["source_size"] - os.path.getsize(output)
        logger.info(f"[TRANSCODE_WORKER] Done: {job.get('title')} saved {saved / 1e9:.2f} GB")

    # ---------- encoding ----------

    def _encode(self, job: Dict[str, Any], src_probe: Dict, plan: Dict, src: str, dst: str) -> None:
        try:
            self._run_encode(job, src_probe, plan, src, dst, force_sw=False)
        except JobFailed as e:
            if e.code != "encoder_crash" or plan.get("decode") != "cuda" or self.cfg.encoder != "hevc_nvenc":
                raise
            logger.info(f"[TRANSCODE_WORKER] GPU decode failed for {job.get('title')}; retrying with software decode")
            self._run_encode(job, src_probe, plan, src, dst, force_sw=True)

    def _run_encode(self, job: Dict[str, Any], src_probe: Dict, plan: Dict, src: str, dst: str, force_sw: bool) -> None:
        args = build_encode_args(src_probe, plan, src, dst, encoder=self.cfg.encoder, ffmpeg=self.cfg.ffmpeg,
                                 force_sw_decode=force_sw)
        duration = float((src_probe.get("format") or {}).get("duration") or 0)
        progress: Dict[str, str] = {}
        proc = runner.FfmpegProcess(args, on_progress=progress.update, popen=self.popen).start()
        now = self.clock()
        last_report, last_gpu = now - self.cfg.progress_interval, now - self.cfg.gpu_check_interval
        server_pause, gpu_reason, last_contention, paused_since = False, None, None, None
        try:
            while proc.poll() is None:
                self.sleep(self.cfg.poll_interval)
                now = self.clock()
                if self.stop_requested:
                    raise JobFailed("worker_shutdown", "worker stopping")
                if now - last_report >= self.cfg.progress_interval:
                    last_report = now
                    resp = self._report_progress(job["id"], progress, duration)
                    if resp.get("cancel"):
                        raise JobFailed("worker_shutdown", "cancelled by admin")
                    server_pause = bool(resp.get("pause"))
                if self.cfg.encoder == "hevc_nvenc" and now - last_gpu >= self.cfg.gpu_check_interval:
                    last_gpu = now
                    # A SIGSTOPped ffmpeg still holds its NVENC session, so it's always ours to subtract.
                    gpu_reason = gpu.contention(self.gpu_query(), own_sessions=1)
                    if gpu_reason:
                        last_contention = now
                gpu_wait = bool(gpu_reason) or (last_contention is not None and now - last_contention < RESUME_AFTER_CLEAR_SECONDS)
                if (server_pause or gpu_wait) and not proc.paused:
                    logger.info(f"[TRANSCODE_WORKER] Pausing encode: {gpu_reason or 'paused by server'}")
                    proc.pause()
                    paused_since = now
                elif proc.paused and not server_pause and not gpu_wait:
                    logger.info("[TRANSCODE_WORKER] Resuming encode")
                    proc.resume()
                    paused_since = None
                if proc.paused and paused_since is not None and now - paused_since > self.cfg.max_pause_seconds:
                    raise JobFailed("paused_too_long", "GPU stayed busy too long")
        except BaseException:
            proc.terminate()
            raise
        rc = proc.wait()
        if rc != 0:
            errors = proc.errors
            if any(marker in errors for marker in GPU_BUSY_MARKERS):
                raise JobFailed("gpu_busy", errors[-1000:])
            raise JobFailed("encoder_crash", f"ffmpeg exited {rc}: {errors[-1500:]}")

    def _report_progress(self, job_id: str, progress: Dict[str, str], duration: float) -> Dict[str, Any]:
        frac = progress_fraction(progress.get("out_time_us"), duration)
        try:
            fps = float(progress.get("fps") or 0) or None
        except ValueError:
            fps = None
        try:
            speed = float((progress.get("speed") or "").rstrip("x") or 0)
        except ValueError:
            speed = 0
        eta = int(duration * (1 - frac) / speed) if frac is not None and speed > 0 else None
        try:
            resp = self.client.progress(job_id, round(frac * 100, 1) if frac is not None else None, fps, eta)
        except ApiError as e:
            if e.status == 409:  # lease lost / job no longer ours
                raise JobFailed("worker_lost", f"server rejected progress: {e.detail}")
            raise
        if self._lease is not None:
            self._lease.touch()
        return resp

    def _sample_check(self, job: Dict[str, Any], settings: Dict[str, Any], src_probe: Dict, plan: Dict,
                      src: str, scratch: List[str]) -> None:
        """Encode 3 short video-only samples; skip early if the file clearly won't shrink enough."""
        duration = float((src_probe.get("format") or {}).get("duration") or 0)
        video_bps = plan.get("max_video_bps") or 0
        size = job["source_size"]
        if duration < SAMPLE_MIN_DURATION or not video_bps or not size:
            return
        sample_bytes, sampled_seconds = 0, 0
        for i, pos in enumerate(SAMPLE_POSITIONS):
            out = os.path.join(self.cfg.scratch_dir, f"{job['id']}.sample{i}.mkv")
            scratch.append(out)
            args = build_encode_args(src_probe, plan, src, out, encoder=self.cfg.encoder, ffmpeg=self.cfg.ffmpeg,
                                     start=duration * pos, duration=SAMPLE_SECONDS)
            rc, _, err = runner.run_capture(args, timeout=600, run=self.run)
            if rc != 0:
                if any(marker in err for marker in GPU_BUSY_MARKERS):
                    raise JobFailed("gpu_busy", err[-500:])
                logger.info(f"[TRANSCODE_WORKER] Sample encode failed ({rc}); skipping the savings pre-check")
                return
            sample_bytes += os.path.getsize(out)
            sampled_seconds += SAMPLE_SECONDS
            fsops.remove_quietly(out)
        ratio = (sample_bytes * 8) / (video_bps * sampled_seconds)
        video_bytes = min(size, video_bps * duration / 8)
        projected = size - video_bytes * (1 - ratio)
        savings_pct = 100 * (1 - projected / size)
        needed = settings.get("min_savings_pct", 10) * SAMPLE_SAFETY_FACTOR
        logger.info(f"[TRANSCODE_WORKER] {job.get('title')}: samples predict {savings_pct:.0f}% savings")
        if savings_pct < needed:
            raise JobFailed("predicted_not_smaller", f"samples predict only {savings_pct:.0f}% savings (need {needed:.0f}%)")

    def _decode_errors(self, path: str) -> Optional[str]:
        rc, _, err = runner.run_capture(build_verify_decode_args(path, self.cfg.ffmpeg), timeout=7200, run=self.run)
        # Only decoder complaints count. The null muxer warns about repeated
        # timestamps on variable-frame-rate sources (XviD, phone MP4s) - those
        # say nothing about whether the HEVC stream decodes.
        lines = [line for line in (err or "").splitlines()
                 if line.strip() and not line.startswith(MUXER_NOISE_PREFIXES)
                 and "non monotonically increasing dts" not in line]
        if rc != 0 or lines:
            return "\n".join(lines)[-1000:] or f"decoder exited {rc}"
        return None

    # ---------- crash recovery ----------

    def recover_inflight(self) -> None:
        for job in self.client.inflight():
            self._keep_lease(job["id"])  # re-hashing a big file can outlast the lease
            try:
                self._recover(job)
            except Exception as e:
                logger.exception(f"[TRANSCODE_WORKER] Recovery of {job.get('id')} failed")
                self._report_failure(job["id"], "needs_manual_review", f"recovery error: {e!r}")
            finally:
                self._release_lease()

    def _recover(self, job: Dict[str, Any]) -> None:
        src = job["source_path"]
        output = job.get("output_path") or fsops.output_path_for(src)
        temp = job.get("temp_path") or fsops.temp_path_for(output, job["id"])
        backup = job.get("backup_path") or fsops.backup_path_for(src)
        backup = fsops.find_backup(backup) or backup
        same = output == src
        exists = {"source": os.path.exists(src), "backup": os.path.exists(backup),
                  "temp": os.path.exists(temp), "output": os.path.exists(output)}
        expected = job.get("output_hash")
        hash_ok = None
        if expected:
            check = temp if exists["temp"] else (output if exists["output"] else None)
            hash_ok = bool(check) and fsops.hash_file(check) == expected
        action = recovery.plan_recovery(job.get("phase") or "queued", exists, same, hash_ok)
        logger.info(f"[TRANSCODE_WORKER] Recovering {job.get('title')} (phase {job.get('phase')}): {action}")
        for name in os.listdir(self.cfg.scratch_dir):
            if name.startswith(job["id"]):
                fsops.remove_quietly(os.path.join(self.cfg.scratch_dir, name))

        if action == recovery.CLEANUP_RETRY:
            if not exists["backup"]:
                fsops.remove_quietly(temp)
            self.client.fail(job["id"], "worker_lost", "worker restarted before the swap; original untouched")
        elif action in (recovery.FINISH_SWAP, recovery.COMPLETE):
            if action == recovery.FINISH_SWAP:
                os.rename(temp, output)
                fsops.fsync_dir(os.path.dirname(output))
                self._phase(job["id"], "swapped")
            settings = self.settings_by_type.get(job.get("media_type") or "movie", {})
            self._finish(job, output, expected, backup, settings, {"recovered": True})
        elif action == recovery.ROLLBACK:
            if exists["output"] and (same or not exists["source"]):
                fsops.remove_quietly(output)
            fsops.remove_quietly(temp)
            os.rename(backup, src)
            fsops.fsync_dir(os.path.dirname(src))
            self.client.fail(job["id"], "worker_lost", "swap interrupted; original restored")
        else:
            self.client.fail(job["id"], "needs_manual_review",
                             f"unexpected files on disk {exists}; nothing was changed")


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="media-manager H.265 transcode worker")
    parser.add_argument("--once", action="store_true", help="process at most one job, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    cfg = WorkerConfig.from_env()
    client = WorkerClient(cfg.api_url, cfg.token, cfg.worker_id, cfg.version, tls_verify=cfg.tls_verify)
    worker = Worker(cfg, client)
    signal.signal(signal.SIGTERM, worker.request_stop)
    signal.signal(signal.SIGINT, worker.request_stop)
    result = worker.run_forever(once=args.once)
    logger.info(f"[TRANSCODE_WORKER] Exiting ({result})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
