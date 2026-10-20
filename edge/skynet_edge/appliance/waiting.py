"""What ``skynet-edge run`` does before the node is enrolled.

The appliance starts ``skynet-edge.service`` at every boot, enrolled or
not. Without credentials the node sends nothing (no heartbeats: the server
would reject them anyway), logs one line a minute, keeps systemd's watchdog
fed, publishes ``node.json`` for the setup page and serves the aiming
preview (opening the camera only while someone looks at it). As soon as a
valid ``credentials.json`` appears it exits with status 0; systemd restarts
the service, whose ``ExecStartPre`` steps then join the tailnet and run
autoconfig with the new identity.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional

from .. import __version__
from ..config import EdgeConfig
from ..health import SdNotifier
from .credentials import load_credentials
from .tap import FrameTap, write_node_status

log = logging.getLogger(__name__)

CAMERA_IDLE_CLOSE_S = 60.0


class PreviewCamera:
    """Opens the configured camera on demand and feeds frames to a tap (waiting mode only)."""

    def __init__(self, cfg: EdgeConfig, tap: FrameTap, source_factory: Optional[Callable[[], Any]] = None):
        self.cfg = cfg
        self.tap = tap
        self._factory = source_factory
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="preview-camera", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5.0)
            self._thread = None

    def _run(self) -> None:
        if self._factory is not None:
            source = self._factory()
        else:
            from ..capture import make_source

            source = make_source(self.cfg)
        try:
            source.open()
            self.error = None
        except Exception as exc:  # noqa: BLE001 - reported on the status page
            self.error = f"camera did not open: {exc}"
            log.warning("%s", self.error)
            self._stop.wait(10.0)
            return
        idle_since = time.monotonic()
        try:
            while not self._stop.is_set():
                got = source.read()
                if got is None:
                    self.error = "camera stopped delivering frames"
                    break
                i420, t_ns = got
                h = self.cfg.camera.height
                self.tap.offer(i420[:h], t_ns)
                if self.tap.wants_frames():
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since > CAMERA_IDLE_CLOSE_S:
                    break
        finally:
            source.close()


def _attitude_status(att_source: Any) -> Dict[str, Any]:
    try:
        a = att_source.attitude()
        return {"yaw_deg": round(a.yaw_deg, 2), "pitch_deg": round(a.pitch_deg, 2), "roll_deg": round(a.roll_deg, 2)}
    except Exception:  # noqa: BLE001
        return {}


def wait_for_enrollment(cfg: EdgeConfig, stop: threading.Event, notifier: Optional[SdNotifier] = None,
                        log_interval_s: float = 60.0, poll_s: float = 1.0,
                        camera_factory: Optional[Callable[[], Any]] = None) -> bool:
    """Block until enrolled (True) or stopped (False)."""
    from ..sensors.attitude import make_attitude_source

    notifier = notifier or SdNotifier()
    path = cfg.node.credentials_file
    assert path is not None
    notifier.ready("waiting for enrollment")
    run_dir = cfg.appliance.run_dir
    tap = FrameTap(run_dir, cfg.appliance.preview_width, cfg.appliance.preview_interval_s) if run_dir else None
    camera = PreviewCamera(cfg, tap, camera_factory) if tap else None
    attitude = make_attitude_source(cfg.attitude)
    try:
        attitude.start()
    except Exception as exc:  # noqa: BLE001 - an absent sensor must not stop the wait
        log.warning("orientation sensor: %s", exc)
        from ..sensors.attitude import AttitudeSource

        attitude = AttitudeSource(cfg.attitude)
    next_log = 0.0
    next_status = 0.0
    host = socket.gethostname()
    try:
        while not stop.is_set():
            now = time.monotonic()
            if load_credentials(path) is not None:
                log.info("enrolled: credentials found in %s; restarting with them", path)
                return True
            notifier.watchdog()
            if now >= next_log:
                log.info("not enrolled yet: waiting (open http://uapfinder.local or http://%s.local to enroll)", host)
                notifier.status("waiting for enrollment")
                next_log = now + log_interval_s
            if tap is not None and camera is not None:
                if tap.wants_frames() and not camera.running:
                    camera.start()
            if run_dir and now >= next_status:
                write_node_status(run_dir, {
                    "pid": os.getpid(), "mode": "waiting", "t_ns": time.time_ns(), "node_id": None,
                    "software_version": __version__, "attitude": _attitude_status(attitude),
                    "attitude_source": cfg.attitude.source,
                    "pointing_source": cfg.attitude.pointing_source,
                    "pointing_sigma_deg": cfg.attitude.pointing_sigma_deg,
                    "camera": {"width": cfg.camera.width, "height": cfg.camera.height,
                               "fov_h_deg": cfg.camera.fov_h_deg, "source": cfg.camera.source,
                               "preview_running": bool(camera and camera.running),
                               "error": camera.error if camera else None},
                })
                next_status = now + cfg.appliance.status_interval_s
            stop.wait(poll_s)
        return False
    finally:
        if camera is not None:
            camera.stop()
        try:
            attitude.stop()
        except Exception:  # noqa: BLE001
            pass
