"""Preallocated in-memory ring buffer of the last N seconds of I420 video.

Why this design
---------------
The node keeps full-resolution video in RAM so that, when something
interesting happens (a local trigger or a server ``clip_request`` after a
stereo match), the seconds *before* the trigger can still be saved. At
1920x1080 I420 one frame is 3.1 MB and one second at 60 fps is 187 MB, so
the buffer is allocated once at start-up, checked against a memory budget,
and (optionally) prefaulted so an out-of-memory condition shows up
immediately rather than minutes into a run. On a Jetson the CPU and GPU
share the same 8 GB, so this budget competes with everything else.

"Halting the cache deletion loop" without stalling the camera
-------------------------------------------------------------
The buffer has ``window`` slots for the rolling window plus ``hold`` spare
slots. A *protection* marks a time window ``[t0, t1]`` that an exporter
still has to copy to disk. When the writer evicts the oldest frame of the
window and that frame is protected and not yet exported, the frame keeps
its slot (it becomes *held*) and the writer takes a spare slot instead. No
pixel data moves and the camera thread never waits. Held slots return to
the spare pool as the exporter advances past them. Only if the exporter
falls behind by more than ``hold`` frames (e.g. clips written to a slow SD
card instead of NVMe) is the oldest held frame sacrificed; that is counted
in ``overruns`` and reported in the heartbeat.

Frames are indexed by a monotonically increasing sequence number and by
capture timestamp. The single writer is the capture thread; readers are
the processing thread (zero-copy Y-plane views of recent frames) and the
clip exporter, which copies frames out with a seqlock-style check so a
frame overwritten mid-copy is detected rather than silently corrupted.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np

from .config import ConfigError, EdgeConfig, ring_memory_bytes, ring_slots


@dataclass
class Protection:
    """A time window whose frames must survive until exported."""

    pid: int
    t0_ns: int
    t1_ns: int
    cursor_ns: int  # frames with t <= cursor have been consumed

    def needs(self, t_ns: int) -> bool:
        return self.t0_ns <= t_ns <= self.t1_ns and t_ns > self.cursor_ns


class FrameRing:
    def __init__(self, width: int, height: int, window_frames: int, hold_frames: int = 0, prefault: bool = True):
        if window_frames < 2:
            raise ValueError("ring buffer needs at least two frames")
        self.width = width
        self.height = height
        self.window = window_frames
        self.hold = hold_frames
        self.frame_shape = (height * 3 // 2, width)
        n_slots = window_frames + hold_frames
        self.nbytes = ring_memory_bytes(width, height, n_slots)
        self._buf = np.empty((n_slots,) + self.frame_shape, dtype=np.uint8)
        if prefault:
            # Touch every page now (black frame: Y=16, U=V=128) so the memory is
            # committed at start-up, not on the first lap of the ring.
            self._buf[:, :height] = 16
            self._buf[:, height:] = 128
        self._slot_seq = np.full(n_slots, -1, dtype=np.int64)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._live: Deque[Tuple[int, int, int]] = deque()  # (seq, t_ns, slot), time order
        self._held: "OrderedDict[int, Tuple[int, int]]" = OrderedDict()  # seq -> (t_ns, slot)
        self._free: List[int] = list(range(n_slots - 1, -1, -1))
        self._next_seq = 0
        self._protections: Dict[int, Protection] = {}
        self._next_pid = 1
        self.overruns = 0

    @classmethod
    def from_config(cls, cfg: EdgeConfig) -> "FrameRing":
        window, hold = ring_slots(cfg)
        need = ring_memory_bytes(cfg.camera.width, cfg.camera.height, window + hold)
        budget = int(cfg.ring_buffer.memory_budget_mb * 1024 * 1024)
        if need > budget:
            raise ConfigError(
                f"ring buffer needs {need / 2**20:.0f} MiB ({window}+{hold} frames of "
                f"{cfg.camera.width}x{cfg.camera.height} I420) but ring_buffer.memory_budget_mb is "
                f"{cfg.ring_buffer.memory_budget_mb:.0f}; reduce seconds/hold_seconds or raise the budget")
        return cls(cfg.camera.width, cfg.camera.height, window, hold, cfg.ring_buffer.prefault)

    # --- writer -------------------------------------------------------------

    def write(self, i420: np.ndarray, t_ns: int) -> int:
        """Append a frame; returns its sequence number. Never blocks on readers."""
        if i420.shape != self.frame_shape:
            raise ValueError(f"frame shape {i420.shape} != {self.frame_shape}")
        with self._lock:
            if len(self._live) >= self.window:
                seq0, t0, slot0 = self._live.popleft()
                if self._needed(t0):
                    self._held[seq0] = (t0, slot0)
                else:
                    self._free.append(slot0)
            if not self._free:
                _, (_, sacrificed) = self._held.popitem(last=False)  # oldest held frame is lost
                self.overruns += 1
                self._free.append(sacrificed)
            slot = self._free.pop()
            seq = self._next_seq
            self._next_seq += 1
            self._slot_seq[slot] = seq
        np.copyto(self._buf[slot], i420)
        with self._cond:
            self._live.append((seq, t_ns, slot))
            self._cond.notify_all()
        return seq

    # --- readers ------------------------------------------------------------

    def _live_index(self, seq: int) -> Optional[int]:
        if not self._live:
            return None
        idx = seq - self._live[0][0]
        return idx if 0 <= idx < len(self._live) else None

    def _lookup(self, seq: int) -> Optional[Tuple[int, int]]:
        idx = self._live_index(seq)
        if idx is not None:
            _, t, slot = self._live[idx]
            return t, slot
        return self._held.get(seq)

    def y_plane(self, seq: int) -> Optional[np.ndarray]:
        """Zero-copy view of a recent frame's Y plane (valid while it stays in the window)."""
        with self._lock:
            found = self._lookup(seq)
        return None if found is None else self._buf[found[1], : self.height]

    def copy_frame(self, seq: int, out: np.ndarray) -> Optional[int]:
        """Copy a frame into ``out``; returns its timestamp, or None if gone or overwritten mid-copy."""
        with self._lock:
            found = self._lookup(seq)
        if found is None:
            return None
        t, slot = found
        np.copyto(out, self._buf[slot])
        if self._slot_seq[slot] != seq:
            return None
        return t

    def latest(self) -> Optional[Tuple[int, int]]:
        with self._lock:
            return (self._live[-1][0], self._live[-1][1]) if self._live else None

    def oldest_time_ns(self) -> Optional[int]:
        """Timestamp of the oldest frame still available (held or live)."""
        with self._lock:
            times = [next(iter(self._held.values()))[0]] if self._held else []
            if self._live:
                times.append(self._live[0][1])
            return min(times) if times else None

    def frames_between(self, t0_ns: int, t1_ns: int) -> List[Tuple[int, int]]:
        """``(seq, t_ns)`` of every available frame with t0 <= t <= t1, in time order."""
        with self._lock:
            out = [(s, t) for s, (t, _) in self._held.items() if t0_ns <= t <= t1_ns]
            live = self._live
            lo, hi = 0, len(live)
            while lo < hi:  # first live frame with t >= t0
                mid = (lo + hi) // 2
                if live[mid][1] < t0_ns:
                    lo = mid + 1
                else:
                    hi = mid
            for i in range(lo, len(live)):
                s, t, _ = live[i]
                if t > t1_ns:
                    break
                out.append((s, t))
        out.sort()
        return out

    def wait_for_frame(self, after_t_ns: int, timeout: float) -> bool:
        """Block until a frame newer than ``after_t_ns`` exists (or timeout)."""
        with self._cond:
            return self._cond.wait_for(lambda: bool(self._live) and self._live[-1][1] > after_t_ns, timeout)

    # --- protections --------------------------------------------------------

    def _needed(self, t_ns: int) -> bool:
        return any(p.needs(t_ns) for p in self._protections.values())

    def protect(self, t0_ns: int, t1_ns: int) -> Protection:
        with self._lock:
            p = Protection(self._next_pid, t0_ns, t1_ns, cursor_ns=t0_ns - 1)
            self._next_pid += 1
            self._protections[p.pid] = p
            return p

    def extend(self, p: Protection, t1_ns: int) -> None:
        with self._lock:
            p.t1_ns = max(p.t1_ns, t1_ns)

    def truncate(self, p: Protection, t1_ns: int) -> None:
        """Shorten a protection (used on shutdown to export only what is buffered)."""
        with self._lock:
            p.t1_ns = min(p.t1_ns, t1_ns)
            self._release_unneeded()

    def advance(self, p: Protection, cursor_ns: int) -> None:
        """Mark frames up to ``cursor_ns`` as exported and free held slots nobody needs."""
        with self._lock:
            p.cursor_ns = max(p.cursor_ns, cursor_ns)
            self._release_unneeded()

    def release(self, p: Protection) -> None:
        with self._lock:
            self._protections.pop(p.pid, None)
            self._release_unneeded()

    def _release_unneeded(self) -> None:
        for seq in [s for s, (t, _) in self._held.items() if not self._needed(t)]:
            _, slot = self._held.pop(seq)
            self._free.append(slot)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"frames": len(self._live), "held": len(self._held), "free_spare": len(self._free),
                    "protections": len(self._protections), "overruns": self.overruns}
