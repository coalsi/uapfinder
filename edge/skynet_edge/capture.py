"""Frame capture: camera/file/synthetic sources and the capture thread.

Every source yields planar I420 frames as one ``(H*3/2, W)`` uint8 array
(Y plane, then U, then V). That is what OpenCV's GStreamer reader returns for
an ``I420`` appsink, what the ring buffer stores and what Y4M/ffmpeg expect,
so a frame is copied exactly once (into the ring) on its way from the
camera to a clip.

Timestamps
----------
Live frames are stamped ``time.time_ns() - capture_latency_ns`` as soon as
``VideoCapture.read()`` returns. That is only as good as two things the
operator must take care of:

* the system clock must be disciplined by GPS + PPS through chrony
  (``deploy/jetson`` configures this). Without PPS, NMEA-only time is
  typically good to tens of milliseconds; NTP over the internet to a few ms.
* ``camera.capture_latency_ms`` must be calibrated once per camera/pipeline
  (sensor exposure midpoint to ``read()`` return), e.g. by filming a GPS-
  disciplined LED or a millisecond clock on a phone that is NTP-synced. On
  Jetson CSI pipelines it is typically tens of milliseconds.

The capture thread does nothing except read, stamp and copy into the ring,
so the GStreamer ``appsink`` queue (``max-buffers=2 drop=true``) stays empty
and the stamp is taken within a frame period of the real arrival.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional, Tuple

import cv2
import numpy as np

from .config import CameraConfig

log = logging.getLogger(__name__)


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
            self._pace_t0 = time.monotonic()
            file_fps = cap.get(cv2.CAP_PROP_FPS)
            if file_fps and abs(file_fps - cfg.fps) > 0.01:
                log.warning("video file is %.3f fps, camera.fps is %.3f; timestamps follow the file", file_fps, cfg.fps)
        if not cap.isOpened():
            raise RuntimeError(f"could not open {self.kind} source "
                               f"({cfg.pipeline() if self.kind == 'gstreamer' else cfg.device if self.kind == 'v4l2' else cfg.file_path})")
        self._cap = cap

    def read(self) -> Optional[Tuple[np.ndarray, int]]:
        if self._cap is None:
            return None
        ok, frame = self._cap.read()
        if not ok and self.kind == "file" and self.cfg.file_loop:
            self._loop_offset_ns = self._last_pos_ns + int(1e9 / self.fps)
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
        if not ok or frame is None:
            return None
        if self.live:
            t_ns = self._clock() - self._latency_ns
        else:
            pos_ms = self._cap.get(cv2.CAP_PROP_POS_MSEC)
            pos_ns = int(round(pos_ms * 1e6)) if pos_ms > 0 else int(self._file_frames * 1e9 / self.fps)
            self._last_pos_ns = self._loop_offset_ns + pos_ns
            t_ns = self._file_start_ns + self._last_pos_ns
            self._file_frames += 1
            if self.cfg.realtime:
                _sleep_until(self._pace_t0 + self._last_pos_ns / 1e9)
        return self.to_i420(frame), t_ns

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


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


class FrameQueue:
    """Bounded hand-off from capture to processing that drops the oldest entry.

    The camera must never wait for the detector: if processing falls behind,
    older frames are skipped (they remain in the ring buffer for clips) and
    the drop is counted for the health report.
    """

    def __init__(self, maxlen: int = 4):
        self._q: Deque[FrameRef] = deque(maxlen=maxlen)
        self._cv = threading.Condition()
        self.dropped = 0
        self.closed = False

    def put(self, ref: FrameRef) -> None:
        with self._cv:
            if len(self._q) == self._q.maxlen:
                self.dropped += 1
            self._q.append(ref)
            self._cv.notify()

    def get(self, timeout: Optional[float] = None) -> Optional[FrameRef]:
        with self._cv:
            if not self._q and not self.closed:
                self._cv.wait(timeout)
            return self._q.popleft() if self._q else None

    def close(self) -> None:
        with self._cv:
            self.closed = True
            self._cv.notify_all()


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
        self.error: Optional[BaseException] = None

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
                seq = self.ring.write(i420, t_ns)
                self._tick()
                self.queue.put(FrameRef(seq, t_ns))
        except BaseException as exc:  # surfaced to the node, which exits non-zero
            self.error = exc
            log.exception("capture thread failed")
        finally:
            self.source.close()
            self.queue.close()

    def _tick(self) -> None:
        now = time.monotonic()
        if self.last_frame_monotonic:
            dt = now - self.last_frame_monotonic
            if dt > 0:
                inst = 1.0 / dt
                self.fps = inst if self.fps == 0.0 else 0.95 * self.fps + 0.05 * inst
        self.last_frame_monotonic = now
        self.frames += 1
