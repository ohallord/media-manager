"""Runs ffmpeg/ffprobe for the worker: progress streaming, pause/resume, capture."""
import json
import signal
import subprocess
import threading
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

from media_manager.transcode.ffmpeg_args import parse_progress_line


class FfmpegProcess:
    """
    One ffmpeg run with `-progress pipe:1`: progress blocks are delivered to
    on_progress from a reader thread; the stderr tail is kept for errors.
    """

    def __init__(self, args: List[str], on_progress: Optional[Callable[[Dict[str, str]], None]] = None,
                 popen=subprocess.Popen):
        self.args = args
        self.on_progress = on_progress
        self._popen = popen
        self.proc = None
        self.stderr_tail: deque = deque(maxlen=200)
        self._threads: List[threading.Thread] = []
        self.paused = False

    def start(self) -> "FfmpegProcess":
        self.proc = self._popen(self.args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        self._threads = [threading.Thread(target=self._read_progress, daemon=True),
                         threading.Thread(target=self._read_stderr, daemon=True)]
        for t in self._threads:
            t.start()
        return self

    def _read_progress(self) -> None:
        block: Dict[str, str] = {}
        for line in self.proc.stdout:
            parsed = parse_progress_line(line)
            if not parsed:
                continue
            key, value = parsed
            block[key] = value
            if key == "progress":  # end of one progress block
                if self.on_progress:
                    try:
                        self.on_progress(dict(block))
                    except Exception:
                        pass  # a reporting hiccup must never kill the encode
                block = {}

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:
            line = line.rstrip()
            if line:
                self.stderr_tail.append(line)

    def poll(self) -> Optional[int]:
        return self.proc.poll()

    def wait(self, timeout: Optional[float] = None) -> int:
        rc = self.proc.wait(timeout=timeout)
        for t in self._threads:
            t.join(timeout=5)
        return rc

    def pause(self) -> None:
        if self.proc and self.proc.poll() is None and not self.paused:
            self.proc.send_signal(signal.SIGSTOP)
            self.paused = True

    def resume(self) -> None:
        if self.proc and self.paused:
            self.proc.send_signal(signal.SIGCONT)
            self.paused = False

    def terminate(self, grace: float = 10.0) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        self.resume()  # a stopped process can't handle SIGTERM
        self.proc.terminate()
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    @property
    def errors(self) -> str:
        return "\n".join(self.stderr_tail)


def run_capture(args: List[str], timeout: Optional[float] = None, run=subprocess.run) -> Tuple[int, str, str]:
    res = run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    return res.returncode, res.stdout, res.stderr


def probe(path: str, ffprobe: str = "ffprobe", count_packets: bool = False, timeout: float = 1800,
          run=subprocess.run) -> Dict:
    """ffprobe JSON; count_packets reads the whole file (exact frame counts for verify)."""
    args = [ffprobe, "-v", "error", "-show_format", "-show_streams"]
    if count_packets:
        args += ["-count_packets"]
    args += ["-of", "json", path]
    rc, out, err = run_capture(args, timeout=timeout, run=run)
    if rc != 0:
        raise RuntimeError(f"ffprobe failed ({rc}): {err.strip()[:500]}")
    return json.loads(out)
