"""The edge node: wires capture, pipeline, sensors, uplink, clips and health together.

Threads and why they are separate:

* ``capture``: reads the camera, stamps and copies each frame into the ring
  buffer. It never waits for anything else, so frame timestamps stay tight.
* main thread: the per-frame ``Pipeline`` and telemetry sends. If it falls
  behind, the capture queue drops the oldest frames (still in the ring).
* ``health``: 1 Hz; systemd watchdog pings (only while frames are being
  processed), heartbeats every ``heartbeat_interval_s``, a periodic status
  log line and the chrony offset.
* uplink receive/batch threads, the clip exporter/encoder threads, the clip
  uploader, ADS-B polling and the GPS/IMU readers.

While any reportable track is active an *event* is recorded: the ring
buffer protects ``[first detection - clips.pre_s, now + clips.post_s]``
("halting the cache deletion loop") and the exporter writes it to disk in
the background, so a later ``clip_request`` from the server is usually
served from that stored clip.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from skynet_common.protocol import MAX_DATAGRAM_BYTES, encode_signed

from . import messages
from .adsb import AdsbFeed
from .capture import CaptureThread, FrameQueue, FrameRef, FrameSource, make_source
from .clips import ClipManager, safe_name
from .config import EdgeConfig
from .geometry import CameraModel
from .health import (SdNotifier, chrony_tracking, headline_temperatures, process_memory_mb, system_memory_mb,
                     thermal_zones_c)
from .pipeline import FrameResult, Pipeline
from .ringbuffer import FrameRing
from .sensors.attitude import make_attitude_source
from .sensors.gps import make_position_source
from .uplink import NodeAuth, Uplink, make_uplink
from .uploader import ClipUploader

log = logging.getLogger(__name__)

_STARTUP_GRACE_S = 120.0  # keep the watchdog fed while the camera opens
_CHRONY_INTERVAL_S = 30.0
# Dropped first when a heartbeat would exceed one datagram.
_OPTIONAL_STATUS_KEYS = ("uplink", "filtered", "stage_ms", "clock_leap", "clock_stratum", "mem_avail_mb")


class EdgeNode:
    def __init__(self, cfg: EdgeConfig, source: Optional[FrameSource] = None, uplink: Optional[Uplink] = None,
                 clock: Callable[[], int] = time.time_ns):
        self.cfg = cfg
        self.node_id = cfg.node.node_id
        self.key = cfg.node.load_key()
        self._clock = clock
        self.cam = CameraModel.from_config(cfg.camera)
        self.ring = FrameRing.from_config(cfg)
        self.source = source if source is not None else make_source(cfg)
        self.queue = FrameQueue(4, block=not self.source.live)
        self.stop_event = threading.Event()
        self.capture = CaptureThread(self.source, self.ring, self.queue, self.stop_event)
        self.pipeline = Pipeline(cfg, self.cam)
        self.gps = make_position_source(cfg.gps)
        self.attitude = make_attitude_source(cfg.attitude)
        self.adsb = AdsbFeed(cfg.adsb, position=self._observer)
        self.clips = ClipManager(self.ring, cfg.clips, cfg.camera.fps)
        self.uploader = ClipUploader(NodeAuth(self.node_id, self.key, clock), self.clips, cfg.server, cfg.clips)
        if uplink is None:
            uplink = make_uplink(cfg.server, self.node_id, self.key, on_command=self.uploader.submit)
        elif uplink.on_command is None:
            uplink.on_command = self.uploader.submit
        self.uplink = uplink
        self.notifier = SdNotifier()
        self.counters: Dict[str, int] = {"frames": 0, "skipped": 0, "telemetry": 0, "no_fix": 0, "events": 0}
        self.proc_fps = 0.0
        self._last_proc = 0.0
        self._started = 0.0
        self._event_id: Optional[str] = None
        self._chrony: Optional[Dict[str, Any]] = None
        self._health_thread: Optional[threading.Thread] = None
        self._warned_no_fix = 0.0

    # --- lifecycle ------------------------------------------------------------

    def stop(self) -> None:
        """Ask the node to shut down (safe from signal handlers and other threads)."""
        self.stop_event.set()

    def run(self, drain_timeout_s: float = 30.0) -> int:
        """Run until stopped or the source ends; returns a process exit code."""
        self._start()
        try:
            self._loop()
        finally:
            self._shutdown(drain_timeout_s)
        if self.capture.error is not None:
            log.error("capture failed: %s", self.capture.error)
            return 1
        return 0

    def _start(self) -> None:
        log.info("node %s starting: %dx%d @ %.1f fps, ring %.0f MiB (%d frames), detector %s, transport %s",
                 self.node_id, self.cam.width, self.cam.height, self.cfg.camera.fps, self.ring.nbytes / 2**20,
                 self.ring.window + self.ring.hold, self.pipeline.detector.name, self.uplink.transport)
        if self.key is None:
            log.warning("no node key configured: messages are unsigned and the server will reject them")
        self._started = time.monotonic()
        self.clips.start()
        self.gps.start()
        self.attitude.start()
        self.adsb.start()
        self.uplink.start()
        self.uploader.start()
        self.capture.start()
        self._health_thread = threading.Thread(target=self._health_loop, name="health", daemon=True)
        self._health_thread.start()

    def _loop(self) -> None:
        ready = False
        while True:
            ref = self.queue.get(timeout=0.5)
            if ref is None:
                if self.queue.finished():
                    return
                continue
            self._process(ref)
            if not ready:
                ready = True
                self.notifier.ready(f"processing {self.cam.width}x{self.cam.height}")

    def _shutdown(self, drain_timeout_s: float) -> None:
        self.notifier.stopping()
        self.stop_event.set()
        self.capture.join(10.0)
        self.clips.stop(timeout=max(10.0, drain_timeout_s))
        if not self.uploader.drain(drain_timeout_s):
            log.warning("shutting down with clip uploads still pending")
        self.uploader.stop()
        self.uplink.stop()
        self.adsb.stop()
        self.gps.stop()
        self.attitude.stop()
        if self._health_thread is not None:
            self._health_thread.join(2.0)
        log.info("node stopped: %s", self.counters)

    # --- per frame ------------------------------------------------------------

    def _observer(self) -> Optional[Tuple[float, float, float]]:
        fix = self.gps.current()
        return None if fix is None else (fix.latitude, fix.longitude, fix.altitude_m)

    def _process(self, ref: FrameRef) -> Optional[FrameResult]:
        y = self.ring.y_plane(ref.seq)
        if y is None:  # overwritten before we got to it: processing is far behind
            self.counters["skipped"] += 1
            return None
        att = self.attitude.attitude()
        fix = self.gps.current()
        observer = None if fix is None else (fix.latitude, fix.longitude, fix.altitude_m)
        aircraft = self.adsb.aircraft(ref.t_ns) if self.adsb.enabled else []
        res = self.pipeline.process(y, ref.t_ns, att, observer, aircraft)
        for r in res.reports:
            if fix is None:
                self.counters["no_fix"] += 1
                continue
            if self.uplink.send(messages.telemetry(self.node_id, fix, att, r)):
                self.counters["telemetry"] += 1
        self._record_event(res)
        self.counters["frames"] += 1
        now = time.monotonic()
        if self._last_proc:
            dt = now - self._last_proc
            if dt > 0:
                self.proc_fps = 1.0 / dt if self.proc_fps == 0.0 else 0.97 * self.proc_fps + 0.03 / dt
        self._last_proc = now
        return res

    def _record_event(self, res: FrameResult) -> None:
        c = self.cfg.clips
        if not c.record_events or not res.reportable:
            return
        end_ns = res.t_ns + int(c.post_s * 1e9)
        if self._event_id is not None and self._event_id in self.clips.active_events():
            self.clips.extend_event(self._event_id, end_ns)
            return
        first_ns = min(tr.first_ns for tr in res.reportable)
        self._event_id = safe_name(f"ev-{first_ns}-{res.reportable[0].id}")
        self.counters["events"] += 1
        self.clips.start_event(self._event_id, first_ns - int(c.pre_s * 1e9), end_ns,
                               {"node_id": self.node_id, "trigger_t_ns": res.t_ns,
                                "tracks": [tr.id for tr in res.reportable]})

    # --- health ---------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        timer = dict(self.pipeline.timer.snapshot())
        timer["ring_write"] = round(self.capture.write_ms, 3)
        mem = process_memory_mb()
        st: Dict[str, Any] = {
            "fps": round(self.capture.fps, 2),
            "proc_fps": round(self.proc_fps, 2),
            "dropped": self.queue.dropped,
            "backend": self.pipeline.detector.name,
            "stage_ms": timer,
            "mem_used_mb": mem["rss_mb"],
            "mem_avail_mb": system_memory_mb()["available_mb"],
            "tracks_active": len(self.pipeline.tracker.tracks),
            "tracks_reported": self.pipeline.reported_tracks,
            "filtered": dict(self.pipeline.labels),
            "ring_overruns": self.ring.overruns,
            "uplink": {k: v for k, v in self.uplink.stats.items() if v},
        }
        st.update(headline_temperatures(thermal_zones_c()))
        st.update(self.gps.status())
        st.update(self.attitude.status())
        if self._chrony:
            st.update(self._chrony)
        return st

    def heartbeat_message(self) -> Optional[Dict[str, Any]]:
        fix = self.gps.current()
        if fix is None:
            return None
        fps = self.capture.fps or self.cfg.camera.fps
        msg = messages.heartbeat(self.node_id, self._clock(), fix, self.attitude.attitude(), self.cam, fps,
                                 self.status())
        for key in _OPTIONAL_STATUS_KEYS:  # keep it within one datagram
            if len(encode_signed(dict(msg, seq=2**62), self.key)) <= MAX_DATAGRAM_BYTES:
                break
            msg["status"].pop(key, None)
        return msg

    def _healthy(self) -> bool:
        now = time.monotonic()
        if self._last_proc:
            return now - self._last_proc <= self.cfg.health.stall_timeout_s
        return now - self._started <= _STARTUP_GRACE_S

    def _health_loop(self) -> None:
        h = self.cfg.health
        next_hb = next_log = next_chrony = time.monotonic()
        while not self.stop_event.is_set():
            now = time.monotonic()
            if h.watchdog and self._healthy():
                self.notifier.watchdog()
            if h.chrony_offset and now >= next_chrony:
                self._chrony = chrony_tracking()
                next_chrony = now + _CHRONY_INTERVAL_S
            if now >= next_hb and (self._last_proc or self.capture.opened.is_set()):
                msg = self.heartbeat_message()
                if msg is not None:
                    self.uplink.send(msg)
                elif now - self._warned_no_fix > 60.0:
                    self._warned_no_fix = now
                    log.warning("no GPS fix and no gps.static position: not sending heartbeats or telemetry")
                next_hb = now + self.cfg.server.heartbeat_interval_s
            if now >= next_log:
                if next_log > self._started:
                    st = self.status()
                    log.info("status: fps %.1f proc %.1f dropped %d tracks %d reported %d filtered %s mem %.0f MB "
                             "stage ms %s", st["fps"], st["proc_fps"], st["dropped"], st["tracks_active"],
                             st["tracks_reported"], st["filtered"], st["mem_used_mb"], st["stage_ms"])
                    self.notifier.status(f"fps {st['proc_fps']:.1f}, tracks {st['tracks_active']}, "
                                         f"reported {st['tracks_reported']}")
                next_log = now + h.status_log_interval_s
            self.stop_event.wait(1.0)
