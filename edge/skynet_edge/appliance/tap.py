"""Frames for other processes without sharing the camera.

Only ``skynet-edge run`` ever opens the camera. Other appliance services
ask it for frames through files in ``/run/skynet`` (a tmpfs):

* preview: the setup page touches ``preview.request`` while someone is
  looking; for 30 s after each touch the node writes ``preview.jpg``
  (grey, ``preview_width`` wide) about once per second.
* stack: star calibration writes ``stack.request``
  (``{"id": "...", "frames": 24, "every": 5}``); the node averages that many
  luma planes, one every ``every`` frames, and writes ``stack.npy``
  (float32 mean) followed by ``stack.json`` (``{"id", "n", "t_ns",
  "t_first_ns", "t_last_ns", "width", "height"}``, ``t_ns`` = mid-exposure).

Writes are atomic (temporary file + rename), so readers never see half a
frame. The cost while nobody asks is one ``stat`` every half second.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from .util import atomic_write, write_json

log = logging.getLogger(__name__)

PREVIEW_HOLD_S = 30.0
MAX_STACK_FRAMES = 200


class FrameTap:
    def __init__(self, run_dir: str, preview_width: int = 640, preview_interval_s: float = 1.0,
                 clock=time.monotonic, wall=time.time):
        self.dir = Path(run_dir)
        self.preview_width = preview_width
        self.preview_interval_s = preview_interval_s
        self._clock = clock
        self._wall = wall
        self._next_check = 0.0
        self._preview_until = 0.0
        self._next_preview = 0.0
        self._stack: Optional[Dict[str, Any]] = None
        self._done_ids: set = set()
        self.previews = 0
        self.stacks = 0

    # --- requests ----------------------------------------------------------------

    def _check_requests(self) -> None:
        now = self._clock()
        if now < self._next_check:
            return
        self._next_check = now + 0.5
        try:
            age = self._wall() - (self.dir / "preview.request").stat().st_mtime
            if age < PREVIEW_HOLD_S:
                self._preview_until = now + (PREVIEW_HOLD_S - age)
        except OSError:
            pass
        if self._stack is None:
            req = self._read_stack_request()
            if req is not None:
                self._stack = req
                log.info("frame stack %s: averaging %d frames, one every %d", req["id"], req["frames"], req["every"])

    def _read_stack_request(self) -> Optional[Dict[str, Any]]:
        p = self.dir / "stack.request"
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        rid = str(raw.get("id", ""))[:64]
        if not rid or rid in self._done_ids:
            return None
        frames = raw.get("frames", 24)
        every = raw.get("every", 1)
        if not isinstance(frames, int) or not isinstance(every, int) or not 1 <= frames <= MAX_STACK_FRAMES \
                or not 1 <= every <= 120:
            log.warning("ignoring malformed stack request")
            self._done_ids.add(rid)
            return None
        return {"id": rid, "frames": frames, "every": every, "seen": 0, "n": 0, "sum": None, "t_first": 0,
                "t_last": 0}

    def wants_frames(self) -> bool:
        """True while a preview or stack is wanted (used to open the camera in waiting mode)."""
        self._check_requests()
        return self._stack is not None or self._clock() < self._preview_until

    # --- per frame -------------------------------------------------------------------

    def offer(self, y: np.ndarray, t_ns: int) -> None:
        """Hand over one luma plane (read only, not kept)."""
        self._check_requests()
        now = self._clock()
        if now < self._preview_until and now >= self._next_preview:
            self._next_preview = now + self.preview_interval_s
            self._write_preview(y)
        if self._stack is not None:
            self._accumulate(y, t_ns)

    def _write_preview(self, y: np.ndarray) -> None:
        try:
            import cv2
        except ImportError:
            return
        h, w = y.shape[:2]
        pw = min(self.preview_width, w)
        ph = max(2, int(round(h * pw / w)))
        small = cv2.resize(y, (pw, ph), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if ok:
            try:
                atomic_write(self.dir / "preview.jpg", buf.tobytes(), 0o640)
                self.previews += 1
            except OSError as exc:
                log.debug("preview write failed: %s", exc)

    def _accumulate(self, y: np.ndarray, t_ns: int) -> None:
        st = self._stack
        assert st is not None
        st["seen"] += 1
        if (st["seen"] - 1) % st["every"]:
            return
        if st["sum"] is None:
            st["sum"] = np.zeros(y.shape[:2], dtype=np.float64)
            st["t_first"] = t_ns
        st["sum"] += y
        st["n"] += 1
        st["t_last"] = t_ns
        if st["n"] >= st["frames"]:
            self._finish_stack()

    def _finish_stack(self) -> None:
        st = self._stack
        assert st is not None
        self._stack = None
        self._done_ids.add(st["id"])
        mean = (st["sum"] / st["n"]).astype(np.float32)
        tmp = self.dir / f".stack.{os.getpid()}.npy"
        try:
            np.save(tmp, mean)
            os.chmod(tmp, 0o640)
            os.replace(tmp, self.dir / "stack.npy")
            write_json(self.dir / "stack.json", {
                "id": st["id"], "n": st["n"], "t_ns": (st["t_first"] + st["t_last"]) // 2,
                "t_first_ns": st["t_first"], "t_last_ns": st["t_last"], "width": int(mean.shape[1]),
                "height": int(mean.shape[0])}, mode=0o640)
            self.stacks += 1
            log.info("frame stack %s written (%d frames)", st["id"], st["n"])
        except OSError as exc:
            log.warning("cannot write frame stack: %s", exc)
            try:
                tmp.unlink()
            except OSError:
                pass


def write_node_status(run_dir: str, status: Dict[str, Any]) -> None:
    try:
        write_json(Path(run_dir) / "node.json", status, mode=0o644)
    except OSError as exc:
        log.debug("cannot write node status: %s", exc)


class ChangeWatcher:
    """Notices when any of a set of files appears, disappears or changes (mtime/size)."""

    def __init__(self, paths, interval_s: float = 10.0, clock=time.monotonic):
        self.paths = [Path(p) for p in paths]
        self.interval_s = interval_s
        self._clock = clock
        self._next = 0.0
        self._sig = self._signature()

    def _signature(self):
        sig = []
        for p in self.paths:
            try:
                st = p.stat()
                sig.append((st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append(None)
        return sig

    def changed(self) -> bool:
        now = self._clock()
        if now < self._next:
            return False
        self._next = now + self.interval_s
        sig = self._signature()
        if sig != self._sig:
            self._sig = sig
            return True
        return False
