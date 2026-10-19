"""Frame capture: camera/file/synthetic sources and the capture thread.

Every source yields planar I420 frames as one ``(H*3/2, W)`` uint8 array
(Y plane, then U, then V). That is what OpenCV's GStreamer reader returns for
an ``I420`` appsink (``cap_gstreamer.cpp`` copies the three planes into one
single-channel Mat of height 1.5 H), what ``gst-launch-1.0 ... ! fdsink``
writes, what the ring buffer stores and what Y4M/ffmpeg expect, so a frame is
copied exactly once (into the ring) on its way from the camera to a clip.

Timestamps
----------
Live frames are stamped ``time.time_ns() - capture_latency_ns`` as soon as
the frame has been read. That is only as good as two things the operator
must take care of:

* the system clock must be disciplined by GPS + PPS through chrony
  (``deploy/jetson`` configures this). Without PPS, NMEA-only time is
  typically good to tens of milliseconds; NTP over the internet to a few ms.
* ``camera.capture_latency_ms`` must be calibrated once per camera/pipeline
  (sensor exposure midpoint to read completion), e.g. by filming an LED
  driven by the GPS PPS output. On Jetson CSI pipelines it is typically
  tens of milliseconds. Uncalibrated latency is a direction error of
  ``angular speed x latency`` along the track on every node.

The capture thread does nothing except read, stamp and copy into the ring,
so the GStreamer queue (``max-buffers=2 drop=true``) stays empty and the
stamp is taken within a frame period of the real arrival. Offline sources
(files, synthetic) instead block on a full queue so every frame is
processed and results are reproducible.
"""

from __future__ import annotations

import fcntl
import logging
import re
import shlex
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from .config import CameraConfig, EdgeConfig

log = logging.getLogger(__name__)

_F_SETPIPE_SZ = getattr(fcntl, "F_SETPIPE_SZ", 1031)


@dataclass
class FrameRef:
    """A captured frame, identified by its ring-buffer sequence number."""

    seq: int
    t_ns: int


class FrameSource(ABC):
    """A source of I420 frames with capture timestamps."""

    live: bool = True

    def __init__(self, width: int, height: int, fps: float):
        self.width = width
        self.height = height
        self.fps = fps

    def open(self) -> None:
        """Acquire the device; may raise."""

    @abstractmethod
    def read(self) -> Optional[Tuple[np.ndarray, int]]:
        """Next ``(i420, t_ns)``; None on end of stream or device failure."""

    def close(self) -> None:
        """Release the device."""

    def to_i420(self, frame: np.ndarray) -> np.ndarray:
        """Normalise whatever OpenCV returned to an I420 array of the configured size."""
        h, w = self.height, self.width
        if frame.ndim == 2 and frame.shape == (h * 3 // 2, w):
            return frame
        if frame.ndim == 2:
            if frame.shape != (h, w):
                frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
            out = np.full((h * 3 // 2, w), 128, dtype=np.uint8)
            out[:h] = frame
            return out
        if frame.shape[:2] != (h, w):
            frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        code = cv2.COLOR_BGRA2YUV_I420 if frame.shape[2] == 4 else cv2.COLOR_BGR2YUV_I420
        return cv2.cvtColor(frame, code)


def opencv_has_gstreamer() -> bool:
    """True if this OpenCV build can open GStreamer pipelines."""
    return re.search(r"GStreamer:\s+YES", cv2.getBuildInformation()) is not None


class OpenCVSource(FrameSource):
    """GStreamer pipeline, V4L2 device or video file through cv2.VideoCapture."""

    def __init__(self, cfg: CameraConfig, clock: Callable[[], int] = time.time_ns):
        super().__init__(cfg.width, cfg.height, cfg.fps)
        self.cfg = cfg
        self.kind = cfg.source
        self.live = cfg.source != "file"
        self._clock = clock
        self._latency_ns = int(cfg.capture_latency_ms * 1e6)
        self._cap: Optional[cv2.VideoCapture] = None
        self._file_start_ns = 0
        self._file_frames = 0
        self._pace_t0 = 0.0
        self._loop_offset_ns = 0
        self._last_pos_ns = 0

    def _describe(self) -> str:
        if self.kind == "gstreamer":
            return self.cfg.pipeline()
        return self.cfg.device if self.kind == "v4l2" else str(self.cfg.file_path)

    def open(self) -> None:
        cfg = self.cfg
        if self.kind == "gstreamer":
            cap = cv2.VideoCapture(cfg.pipeline(), cv2.CAP_GSTREAMER)
        elif self.kind == "v4l2":
            cap = cv2.VideoCapture(cfg.device, cv2.CAP_V4L2)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
            cap.set(cv2.CAP_PROP_FPS, cfg.fps)
        else:
            cap = cv2.VideoCapture(str(cfg.file_path))
            self._file_start_ns = cfg.file_start_ns if cfg.file_start_ns is not None else self._clock()
            self._file_frames = 0
            self._pace_t0 = time.monotonic()
            file_fps = cap.get(cv2.CAP_PROP_FPS)
            if file_fps and abs(file_fps - cfg.fps) > 0.01:
                log.warning("video file is %.3f fps, camera.fps is %.3f; timestamps follow camera.fps",
                            file_fps, cfg.fps)
        if not cap.isOpened():
            raise RuntimeError(f"could not open {self.kind} source ({self._describe()})")
        self._cap = cap

    def read(self) -> Optional[Tuple[np.ndarray, int]]:
        if self._cap is None:
            return None
        ok, frame = self._cap.read()
        if not ok and self.kind == "file" and self.cfg.file_loop:
            self._loop_offset_ns = self._last_pos_ns + int(1e9 / self.fps)
            self._file_frames = 0
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
        if not ok or frame is None:
            return None
        if self.live:
            t_ns = self._clock() - self._latency_ns
        else:
            # Frame index / camera.fps, not CAP_PROP_POS_MSEC: container
            # timestamps of re-encoded clips are rounded and some demuxers
            # report the *next* frame's time.
            self._last_pos_ns = self._loop_offset_ns + int(round(self._file_frames * 1e9 / self.fps))
            t_ns = self._file_start_ns + self._last_pos_ns
            self._file_frames += 1
            if self.cfg.realtime:
                _sleep_until(self._pace_t0 + self._last_pos_ns / 1e9)
        return self.to_i420(frame), t_ns

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


def gst_launch_command(cfg: CameraConfig) -> List[str]:
    """``gst-launch-1.0`` argv that writes the configured pipeline's raw I420 to stdout.

    The trailing ``appsink`` of the pipeline is replaced by a leaky queue and
    ``fdsink``, so a stalled reader drops frames instead of blocking the
    camera (the same behaviour as ``appsink drop=true``).
    """
    parts = [p.strip() for p in cfg.pipeline().split("!")]
    if parts and parts[-1].startswith("appsink"):
        parts = parts[:-1]
    parts += ["queue leaky=downstream max-size-buffers=2 max-size-bytes=0 max-size-time=0",
              "fdsink fd=1 sync=false"]
    return [cfg.gst_launch, "-q"] + shlex.split(" ! ".join(parts))


class PipeSource(FrameSource):
    """Raw I420 frames from a child process's stdout (``gst-launch-1.0``, ``ffmpeg``).

    Used when OpenCV was built without GStreamer: the pipeline runs in
    ``gst-launch-1.0`` and this class reads exactly W*H*3/2 bytes per frame.
    NOT hardware-tested with nvarguscamerasrc; tested here with ffmpeg.
    """

    def __init__(self, argv: Sequence[str], width: int, height: int, fps: float, latency_ms: float = 0.0,
                 clock: Callable[[], int] = time.time_ns):
        super().__init__(width, height, fps)
        self.argv = list(argv)
        self._clock = clock
        self._latency_ns = int(latency_ms * 1e6)
        self._proc: Optional[subprocess.Popen] = None
        self._frame = np.empty((height * 3 // 2, width), dtype=np.uint8)
        self._view = memoryview(self._frame).cast("B")

    def open(self) -> None:
        self._proc = subprocess.Popen(self.argv, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, bufsize=0)
        assert self._proc.stdout is not None
        try:
            fcntl.fcntl(self._proc.stdout.fileno(), _F_SETPIPE_SZ, 1 << 20)
        except OSError:
            pass  # larger pipes only save syscalls

    def read(self) -> Optional[Tuple[np.ndarray, int]]:
        if self._proc is None or self._proc.stdout is None:
            return None
        got, total = 0, len(self._view)
        while got < total:
            n = self._proc.stdout.readinto(self._view[got:])
            if not n:
                return None
            got += n
        return self._frame, self._clock() - self._latency_ns

    def close(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        if self._proc.stdout is not None:
            self._proc.stdout.close()
        self._proc = None


class RenderedSource(FrameSource):
    """Frames from a callable renderer on a synthetic timeline (tests, bench, demos)."""

    live = False

    def __init__(self, width: int, height: int, fps: float, render: Callable[[int, int], np.ndarray],
                 t0_ns: int, n_frames: Optional[int] = None, realtime: bool = False):
        super().__init__(width, height, fps)
        self._render = render
        self.t0_ns = t0_ns
        self.n_frames = n_frames
        self.realtime = realtime
        self._i = 0
        self._pace_t0 = 0.0

    def open(self) -> None:
        self._i = 0
        self._pace_t0 = time.monotonic()

    def frame_time_ns(self, i: int) -> int:
        return self.t0_ns + int(round(i * 1e9 / self.fps))

    def read(self) -> Optional[Tuple[np.ndarray, int]]:
        if self.n_frames is not None and self._i >= self.n_frames:
            return None
        i = self._i
        self._i += 1
        if self.realtime:
            _sleep_until(self._pace_t0 + i / self.fps)
        t_ns = self.frame_time_ns(i)
        return self._render(i, t_ns), t_ns


def _sleep_until(t_monotonic: float) -> None:
    dt = t_monotonic - time.monotonic()
    if dt > 0:
        time.sleep(dt)


def make_source(cfg: EdgeConfig, n_frames: Optional[int] = None) -> FrameSource:
    """Build the configured frame source."""
    cam = cfg.camera
    if cam.source == "synthetic":
        from .synthetic import standard_scene  # rendering code is only needed for this source

        scene = standard_scene(cfg, seed=cam.synthetic_seed)
        t0 = cam.file_start_ns if cam.file_start_ns is not None else time.time_ns()
        return RenderedSource(cam.width, cam.height, cam.fps, scene.render, t0, n_frames, realtime=cam.realtime)
    if cam.source == "gstreamer":
        backend = cam.gstreamer_backend
        if backend == "auto":
            backend = "opencv" if opencv_has_gstreamer() else "subprocess"
        if backend == "subprocess":
            log.info("camera: gst-launch subprocess (OpenCV has no GStreamer support)")
            return PipeSource(gst_launch_command(cam), cam.width, cam.height, cam.fps, cam.capture_latency_ms)
    return OpenCVSource(cam)


class FrameQueue:
    """Bounded hand-off from capture to processing.

    For a live camera the oldest entry is dropped when full: the camera must
    never wait for the detector, the skipped frames remain in the ring buffer
    for clips, and the drop is counted for the health report. Offline sources
    use ``block=True`` so the reader waits instead and every frame is
    processed.
    """

    def __init__(self, maxlen: int = 4, block: bool = False):
        self._q: Deque[FrameRef] = deque()
        self._maxlen = maxlen
        self._block = block
        self._cv = threading.Condition()
        self.dropped = 0
        self.closed = False

    def put(self, ref: FrameRef) -> None:
        with self._cv:
            if self._block:
                self._cv.wait_for(lambda: len(self._q) < self._maxlen or self.closed)
            elif len(self._q) >= self._maxlen:
                self._q.popleft()
                self.dropped += 1
            if not self.closed:
                self._q.append(ref)
            self._cv.notify_all()

    def get(self, timeout: Optional[float] = None) -> Optional[FrameRef]:
        with self._cv:
            if not self._q and not self.closed:
                self._cv.wait(timeout)
            ref = self._q.popleft() if self._q else None
            self._cv.notify_all()
            return ref

    def close(self) -> None:
        """No more frames will be queued; pending ones can still be read."""
        with self._cv:
            self.closed = True
            self._cv.notify_all()

    def finished(self) -> bool:
        with self._cv:
            return self.closed and not self._q


class CaptureThread(threading.Thread):
    """Reads frames, writes them into the ring buffer and queues them for processing.

    Live sources are reopened with exponential backoff if the device fails
    (nvargus-daemon restarts, USB camera unplugged); file and synthetic
    sources end the thread at end of stream and close the queue.
    """

    def __init__(self, source: FrameSource, ring, queue: FrameQueue, stop_event: threading.Event):
        super().__init__(name="capture", daemon=True)
        self.source = source
        self.ring = ring
        self.queue = queue
        self.stop_event = stop_event
        self.frames = 0
        self.read_failures = 0
        self.fps = 0.0
        self.last_frame_monotonic = 0.0
        self.write_ms = 0.0
        self.error: Optional[BaseException] = None
        self.opened = threading.Event()

    def run(self) -> None:
        backoff = 1.0
        opened = False
        try:
            while not self.stop_event.is_set():
                if not opened:
                    try:
                        self.source.open()
                        opened = True
                        backoff = 1.0
                        self.opened.set()
                    except Exception as exc:
                        if not self.source.live:
                            raise
                        log.error("camera open failed: %s; retrying in %.0f s", exc, backoff)
                        self.stop_event.wait(backoff)
                        backoff = min(backoff * 2.0, 30.0)
                        continue
                got = self.source.read()
                if got is None:
                    if not self.source.live:
                        break
                    self.read_failures += 1
                    log.error("camera read failed; reopening")
                    self.source.close()
                    opened = False
                    continue
                i420, t_ns = got
                t0 = time.perf_counter()
                seq = self.ring.write(i420, t_ns)
                self.write_ms = 0.98 * self.write_ms + 0.02 * (time.perf_counter() - t0) * 1e3
                self._tick()
                self.queue.put(FrameRef(seq, t_ns))
        except BaseException as exc:  # surfaced to the node, which exits non-zero
            self.error = exc
            log.exception("capture thread failed")
        finally:
            self.source.close()
            self.queue.close()
            self.opened.set()

    def _tick(self) -> None:
        now = time.monotonic()
        if self.last_frame_monotonic:
            dt = now - self.last_frame_monotonic
            if dt > 0:
                inst = 1.0 / dt
                self.fps = inst if self.fps == 0.0 else 0.95 * self.fps + 0.05 * inst
        self.last_frame_monotonic = now
        self.frames += 1
