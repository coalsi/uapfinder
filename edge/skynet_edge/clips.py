"""Clip export: protected ring-buffer windows -> Y4M on disk -> MP4 (libx264).

Two-stage on purpose. Stage 1 (exporter thread) copies raw I420 frames out
of RAM as fast as the disk allows, so the protected window can be released
and the ring buffer's hold pool drains quickly; raw Y4M needs no CPU beyond
a memcpy. Stage 2 (transcoder thread) runs ffmpeg/libx264 at idle CPU and
I/O priority (``nice -n 19``, ``ionice -c 3``) with a limited thread count
so it never starves the detector.

The Jetson Orin Nano has **no hardware video encoder** (NVIDIA's "Software
Encode in Orin Nano" developer-guide page; NVENC exists on Orin NX/AGX
only), so encoding is CPU-only on this target. That is why encoding is
deferred, low priority, and limited to event clips rather than continuous
recording. Raw 1080p60 I420 is ~187 MB/s, so the clip directory must be on
NVMe; a microSD card cannot sustain it and the ring buffer would report
overruns.

Every clip directory holds ``clip.mp4`` and ``meta.json`` with the exact
capture timestamp of every frame (the MP4 itself is constant frame rate),
which is what forensic analysis and stereo re-processing need.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, List, Optional

import numpy as np

from .config import ClipsConfig
from .ringbuffer import FrameRing, Protection

log = logging.getLogger(__name__)

ClipCallback = Callable[["ClipResult"], None]


class Y4MWriter:
    """Minimal YUV4MPEG2 writer for planar 4:2:0 frames."""

    def __init__(self, path: Path, width: int, height: int, fps: float):
        rate = Fraction(fps).limit_denominator(1001)
        self._f: BinaryIO = open(path, "wb")
        self._f.write(f"YUV4MPEG2 W{width} H{height} F{rate.numerator}:{rate.denominator} Ip A1:1 C420mpeg2\n"
                      .encode("ascii"))
        self.frames = 0

    def write(self, i420: np.ndarray) -> None:
        self._f.write(b"FRAME\n")
        self._f.write(np.ascontiguousarray(i420).data)
        self.frames += 1

    def close(self) -> None:
        self._f.close()


@dataclass
class ClipResult:
    clip_id: str
    kind: str  # "event" or "request"
    directory: str
    ok: bool
    mp4_path: Optional[str] = None
    first_ns: Optional[int] = None
    last_ns: Optional[int] = None
    frames: int = 0
    lost_frames: int = 0
    bytes: int = 0
    sha256: str = ""
    error: str = ""


@dataclass
class _Job:
    clip_id: str
    kind: str
    directory: Path
    protection: Protection
    metadata: Dict[str, Any]
    writer: Optional[Y4MWriter] = None
    timestamps: List[int] = field(default_factory=list)
    lost: int = 0
    last_progress: float = field(default_factory=time.monotonic)
    waiters: List[ClipCallback] = field(default_factory=list)


def ffmpeg_command(cfg: ClipsConfig, src: Path, dst: Path) -> List[str]:
    cmd: List[str] = []
    if cfg.low_priority:
        if shutil.which("nice"):
            cmd += ["nice", "-n", "19"]
        if shutil.which("ionice"):
            cmd += ["ionice", "-c", "3"]
    cmd += [cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "yuv4mpegpipe", "-i", str(src),
            "-c:v", "libx264", "-preset", cfg.x264_preset, "-crf", str(cfg.x264_crf),
            "-pix_fmt", "yuv420p", "-threads", str(cfg.ffmpeg_threads),
            "-movflags", "+faststart", str(dst)]
    return cmd


def safe_name(text: str) -> str:
    """A directory name derived from a (server-supplied) id that cannot escape the clip store."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", text)[:128].lstrip(".")
    return name or "clip"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ClipManager:
    """Exports protected windows to disk, transcodes them and keeps the clip store tidy."""

    def __init__(self, ring: FrameRing, cfg: ClipsConfig, fps: float):
        self.ring = ring
        self.cfg = cfg
        self.fps = fps
        self.root = Path(cfg.storage_dir)
        self._lock = threading.Lock()
        self._jobs: Dict[str, _Job] = {}
        self._stored: Dict[str, ClipResult] = {}
        self._transcode_q: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self._stop = threading.Event()
        self._scratch = np.empty(ring.frame_shape, dtype=np.uint8)
        self._threads: List[threading.Thread] = []
        self.ffmpeg_available = shutil.which(cfg.ffmpeg) is not None

    # --- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        for sub in ("events", "requests"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self._load_store()
        if not self.ffmpeg_available:
            log.warning("ffmpeg not found (%s): clips will stay as raw Y4M and cannot be uploaded", self.cfg.ffmpeg)
        self._threads = [threading.Thread(target=self._export_loop, name="clip-export", daemon=True),
                         threading.Thread(target=self._transcode_loop, name="clip-encode", daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Finish exporting what is buffered, then stop (pending encodes are completed)."""
        latest = self.ring.latest()
        with self._lock:
            for job in self._jobs.values():
                if latest is not None:
                    self.ring.truncate(job.protection, latest[1])
        self._stop.set()
        for t in self._threads[:1]:
            t.join(timeout)
        self._transcode_q.put(None)
        for t in self._threads[1:]:
            t.join(timeout)

    # --- public API -----------------------------------------------------------

    def start_event(self, event_id: str, t_start_ns: int, t_end_ns: int, metadata: Dict[str, Any]) -> None:
        self._start_job(event_id, "event", t_start_ns, t_end_ns, metadata)

    def extend_event(self, event_id: str, t_end_ns: int) -> None:
        with self._lock:
            job = self._jobs.get(safe_name(event_id))
            if job is None:
                return
            cap = job.protection.t0_ns + int((self.cfg.max_event_s + self.cfg.pre_s + self.cfg.post_s) * 1e9)
            self.ring.extend(job.protection, min(t_end_ns, cap))

    def active_events(self) -> List[str]:
        with self._lock:
            return [k for k, j in self._jobs.items() if j.kind == "event"]

    def request_clip(self, request_id: str, t_start_ns: int, t_end_ns: int, on_done: ClipCallback) -> None:
        """Produce a clip for a server request, reusing an event clip when one covers it.

        ``on_done`` runs on an internal thread and must return quickly (the
        uploader just queues the result).
        """
        slack = int(1e9)
        stored: Optional[ClipResult] = None
        with self._lock:
            for res in self._stored.values():
                if res.ok and res.mp4_path and res.first_ns is not None and res.last_ns is not None \
                        and res.first_ns <= t_start_ns + slack and res.last_ns >= t_end_ns - slack:
                    stored = res
                    break
            if stored is None:
                for job in self._jobs.values():
                    if job.kind == "event" and job.protection.t0_ns <= t_start_ns + slack:
                        self.ring.extend(job.protection, t_end_ns)
                        job.waiters.append(on_done)
                        log.info("clip request %s attached to recording event %s", request_id, job.clip_id)
                        return
        if stored is not None:
            log.info("clip request %s served from stored clip %s", request_id, stored.clip_id)
            on_done(stored)
            return
        oldest = self.ring.oldest_time_ns()
        if oldest is None or t_end_ns < oldest:
            on_done(ClipResult(request_id, "request", "", ok=False, error="requested window is no longer buffered"))
            return
        self._start_job(request_id, "request", max(t_start_ns, oldest), t_end_ns,
                        {"requested_t_start_ns": t_start_ns, "requested_t_end_ns": t_end_ns}, on_done)

    def stored_clips(self) -> List[ClipResult]:
        with self._lock:
            return list(self._stored.values())

    # --- internals ------------------------------------------------------------

    def _start_job(self, clip_id: str, kind: str, t0: int, t1: int, metadata: Dict[str, Any],
                   on_done: Optional[ClipCallback] = None) -> None:
        clip_id = safe_name(clip_id)
        directory = self.root / ("events" if kind == "event" else "requests") / clip_id
        directory.mkdir(parents=True, exist_ok=True)
        job = _Job(clip_id, kind, directory, self.ring.protect(t0, t1), dict(metadata))
        if on_done is not None:
            job.waiters.append(on_done)
        with self._lock:
            self._jobs[clip_id] = job
        log.info("recording %s %s [%d, %d]", kind, clip_id, t0, t1)

    def _export_loop(self) -> None:
        while True:
            with self._lock:
                jobs = list(self._jobs.values())
            if not jobs and self._stop.is_set():
                return
            progressed = False
            for job in jobs:
                progressed |= self._pump(job)
            if not progressed:
                latest = self.ring.latest()
                self.ring.wait_for_frame(latest[1] if latest else 0, 0.05)

    def _pump(self, job: _Job) -> bool:
        p = job.protection
        frames = self.ring.frames_between(p.cursor_ns + 1, p.t1_ns)
        for seq, t in frames:
            if job.writer is None:
                job.writer = Y4MWriter(job.directory / "clip.y4m", self.ring.width, self.ring.height, self.fps)
            if self.ring.copy_frame(seq, self._scratch) is None:
                job.lost += 1
            else:
                job.writer.write(self._scratch)
                job.timestamps.append(t)
            self.ring.advance(p, t)
            job.last_progress = time.monotonic()
        latest = self.ring.latest()
        done = latest is not None and latest[1] >= p.t1_ns
        stalled = time.monotonic() - job.last_progress > self.cfg.frame_wait_timeout_s
        if done or stalled or self._stop.is_set():
            if stalled and not done:
                log.warning("clip %s: no frames for %.0f s, finishing early", job.clip_id, self.cfg.frame_wait_timeout_s)
            self._finish_export(job)
        return bool(frames)

    def _finish_export(self, job: _Job) -> None:
        with self._lock:
            self._jobs.pop(job.clip_id, None)
        self.ring.release(job.protection)
        if job.writer is not None:
            job.writer.close()
        self._transcode_q.put(job)

    def _transcode_loop(self) -> None:
        while True:
            job = self._transcode_q.get()
            if job is None:
                return
            result = self._transcode(job)
            if result.ok:
                with self._lock:
                    self._stored[result.clip_id] = result
            for cb in job.waiters:
                try:
                    cb(result)
                except Exception:
                    log.exception("clip callback failed for %s", job.clip_id)
            self.enforce_retention()

    def _transcode(self, job: _Job) -> ClipResult:
        ts = job.timestamps
        # Frames the ring had to sacrifice never reach the exporter; count them from timestamp gaps.
        expected = int(round((ts[-1] - ts[0]) * self.fps / 1e9)) + 1 if ts else 0
        res = ClipResult(job.clip_id, job.kind, str(job.directory), ok=False, frames=len(ts),
                         lost_frames=job.lost + max(0, expected - len(ts)),
                         first_ns=ts[0] if ts else None, last_ns=ts[-1] if ts else None)
        y4m = job.directory / "clip.y4m"
        mp4 = job.directory / "clip.mp4"
        if not ts:
            res.error = "no frames available"
        elif not self.ffmpeg_available:
            res.error = "ffmpeg not available; raw clip.y4m kept"
        else:
            cmd = ffmpeg_command(self.cfg, y4m, mp4)
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0 and mp4.exists():
                res.ok = True
                res.mp4_path = str(mp4)
                res.bytes = mp4.stat().st_size
                res.sha256 = sha256_file(mp4)
                if not self.cfg.keep_y4m:
                    y4m.unlink(missing_ok=True)
            else:
                res.error = f"ffmpeg exit {proc.returncode}: {proc.stderr.strip()[-500:]}"
        if not res.ok:
            log.error("clip %s failed: %s", job.clip_id, res.error)
        meta = dict(job.metadata, result=asdict(res), fps=self.fps, width=self.ring.width,
                    height=self.ring.height, frame_timestamps_ns=ts)
        (job.directory / "meta.json").write_text(json.dumps(meta, separators=(",", ":")), encoding="utf-8")
        return res

    def _load_store(self) -> None:
        for meta_path in self.root.glob("*/*/meta.json"):
            try:
                result = ClipResult(**json.loads(meta_path.read_text(encoding="utf-8"))["result"])
            except (ValueError, KeyError, TypeError):
                continue
            if result.ok and result.mp4_path and Path(result.mp4_path).exists():
                self._stored[result.clip_id] = result

    def enforce_retention(self) -> None:
        """Delete the oldest clip directories beyond the age and size limits."""
        dirs = [d for sub in ("events", "requests") for d in (self.root / sub).glob("*") if d.is_dir()]
        with self._lock:
            active = {str(j.directory) for j in self._jobs.values()}
        dirs = sorted((d for d in dirs if str(d) not in active), key=lambda d: d.stat().st_mtime)
        sizes = {d: sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) for d in dirs}
        total = sum(sizes.values())
        limit = self.cfg.retention_max_gb * 1e9
        cutoff = time.time() - self.cfg.retention_max_age_days * 86400
        for d in dirs:
            if total <= limit and d.stat().st_mtime >= cutoff:
                break
            shutil.rmtree(d, ignore_errors=True)
            total -= sizes[d]
            with self._lock:
                self._stored.pop(d.name, None)
            log.info("retention: deleted %s", d)
