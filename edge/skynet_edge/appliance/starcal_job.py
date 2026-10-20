"""Star-calibration scheduling: ``skynet-edge starcal`` (``uapfinder-starcal.service``, every 30 min).

The service exits at once unless all of these hold (``decide``):

1. the star-calibration module (``skynet_edge.starcal``) can be imported;
2. the node process is running (fresh ``/run/skynet/node.json``): it owns
   the camera and supplies the frames;
3. there is a GPS fix (from the node, else straight from gpsd);
3a. chrony says the clock is synchronised to within ``CLOCK_MAX_OFFSET_MS``
   (a wrong clock looks like a rotated camera to the solver and can give a
   confident but wrong pointing);
4. there is no good calibration yet, or it is more than 7 days old;
5. ``starcal.is_dark_enough(now, observer)`` (Sun more than 12 deg below the horizon).

Otherwise it asks the node for a stack of frames (``tap.py``), calls
``starcal.calibrate(image, t_ns, Observer, Prior)`` and, if the result is
``ok``, writes ``calibration.json`` atomically. The node notices the new
file and restarts itself, so ``autoconfig`` applies the new pointing and
lens model. Every attempt is summarised in ``starcal-last.json`` for the
status page. Clouds or a failed solve just mean another try 30 min later.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional, Tuple

from ..health import chrony_tracking
from .calibration import from_result, load_calibration, save_calibration
from .paths import Paths
from .util import file_age_s, read_json, write_json

log = logging.getLogger(__name__)

MAX_AGE_DAYS = 7.0
NODE_STATUS_MAX_AGE_S = 30.0
FIX_MAX_AGE_S = 120.0
STACK_FRAMES = 24
STACK_EVERY = 5
STACK_TIMEOUT_S = 120.0
PRIOR_IMU_SIGMA_DEG = 15.0
# Without an orientation sensor: the build guide says to aim between 45 deg up
# and straight up, level, so pitch/roll are loosely known and the heading not.
PRIOR_MOUNT = dict(pitch_deg=67.5, pitch_sigma_deg=15.0, roll_deg=0.0, roll_sigma_deg=10.0)
CLOCK_MAX_OFFSET_MS = 1000.0


def load_starcal() -> Optional[Any]:
    """The star-calibration interface, or None if the module is missing or broken."""
    try:
        from .. import starcal  # type: ignore[attr-defined]

        return SimpleNamespace(Observer=starcal.Observer, Prior=starcal.Prior, calibrate=starcal.calibrate,
                               is_dark_enough=starcal.is_dark_enough)
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, whatever is wrong with it
        log.info("star calibration unavailable: %s", exc)
        return None


@dataclass
class Decision:
    run: bool
    reason: str
    observer: Optional[Tuple[float, float, float]] = None


def gpsd_fix(addr: Tuple[str, int] = ("127.0.0.1", 2947), timeout_s: float = 3.0) -> Optional[Dict[str, Any]]:
    from ..sensors.gps import parse_gpsd_tpv

    try:
        sock = socket.create_connection(addr, timeout=1.0)
    except OSError:
        return None
    try:
        sock.settimeout(0.5)
        sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
        buf = b""
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                tpv = parse_gpsd_tpv(obj) if isinstance(obj, dict) else None
                if tpv is not None and tpv["alt"] is not None:
                    return {"latitude": tpv["lat"], "longitude": tpv["lon"], "altitude_m": tpv["alt"],
                            "mode": tpv["mode"], "t_ns": time.time_ns()}
    finally:
        sock.close()
    return None


def decide(now_ns: int, node: Optional[Dict[str, Any]], node_age_s: Optional[float],
           fix: Optional[Dict[str, Any]], calib: Optional[Dict[str, Any]], starcal: Any,
           force: bool = False, max_age_days: float = MAX_AGE_DAYS,
           clock: Optional[Dict[str, Any]] = None) -> Decision:
    if starcal is None:
        return Decision(False, "star calibration module not available")
    if node is None or node_age_s is None or node_age_s > NODE_STATUS_MAX_AGE_S:
        return Decision(False, "node is not running (it supplies the camera frames)")
    if fix is None or int(fix.get("mode", 0)) < 2 or (
            fix.get("t_ns") and (now_ns - int(fix["t_ns"])) / 1e9 > FIX_MAX_AGE_S):
        return Decision(False, "no GPS fix")
    if clock is None or str(clock.get("clock_leap", "")).lower().startswith("not") or (
            abs(float(clock.get("clock_offset_ms") or 0.0)) > CLOCK_MAX_OFFSET_MS):
        return Decision(False, "clock not synchronised yet")
    observer = (float(fix["latitude"]), float(fix["longitude"]), float(fix["altitude_m"]))
    if calib is not None and not force:
        age_days = (now_ns - int(calib["t_ns"])) / 86400e9
        if age_days < max_age_days:
            return Decision(False, f"calibration is {age_days:.1f} days old (re-run after {max_age_days:g})",
                            observer)
    try:
        dark = bool(starcal.is_dark_enough(now_ns, starcal.Observer(*observer)))
    except Exception as exc:  # noqa: BLE001
        return Decision(False, f"darkness check failed: {exc}", observer)
    if not dark:
        return Decision(False, "not dark yet (Sun above -12 deg)", observer)
    return Decision(True, "dark, GPS fix, calibration needed", observer)


def make_prior(starcal: Any, node: Dict[str, Any]) -> Any:
    cam = node.get("camera") or {}
    kwargs: Dict[str, Any] = {"width": int(cam.get("width", 1920)), "height": int(cam.get("height", 1080)),
                              "fov_h_deg": float(cam.get("fov_h_deg", 90.0))}
    att = node.get("attitude") or {}
    if node.get("pointing_source") == "imu" and att:
        kwargs.update(yaw_deg=att.get("yaw_deg"), pitch_deg=att.get("pitch_deg"), roll_deg=att.get("roll_deg"),
                      sigma_deg=PRIOR_IMU_SIGMA_DEG)
    else:
        kwargs.update(PRIOR_MOUNT)  # heading unknown
    return starcal.Prior(**kwargs)


def request_stack(paths: Paths, frames: int = STACK_FRAMES, every: int = STACK_EVERY,
                  timeout_s: float = STACK_TIMEOUT_S, sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic) -> Optional[Tuple[Any, Dict[str, Any]]]:
    """Ask the node for a frame stack; (image, meta) or None on timeout."""
    import numpy as np

    rid = uuid.uuid4().hex[:16]
    write_json(paths.stack_request, {"id": rid, "frames": frames, "every": every}, mode=0o640)
    end = clock() + timeout_s
    while clock() < end:
        meta = read_json(paths.stack_json)
        if isinstance(meta, dict) and meta.get("id") == rid:
            try:
                return np.load(paths.stack_npy, allow_pickle=False), meta
            except (OSError, ValueError) as exc:
                log.warning("cannot read frame stack: %s", exc)
                return None
        sleep(1.0)
    return None


def run_job(paths: Optional[Paths] = None, force: bool = False, starcal: Any = None,
            fix_source: Callable[[], Optional[Dict[str, Any]]] = gpsd_fix, now_ns: Optional[int] = None,
            stacker: Callable[..., Optional[Tuple[Any, Dict[str, Any]]]] = request_stack,
            clock_source: Callable[[], Optional[Dict[str, Any]]] = chrony_tracking) -> Dict[str, Any]:
    """One scheduler run; returns the attempt summary (also written to ``starcal-last.json`` when attempted)."""
    paths = paths or Paths.from_env()
    starcal = starcal if starcal is not None else load_starcal()
    now = time.time_ns() if now_ns is None else now_ns
    node = read_json(paths.node_json)
    node = node if isinstance(node, dict) else None
    fix = (node or {}).get("fix") or None
    if fix is None and starcal is not None and node is not None:
        fix = fix_source()
    calib = load_calibration(paths.calibration)
    clock = clock_source() if starcal is not None and fix is not None else None
    d = decide(now, node, file_age_s(paths.node_json), fix, calib, starcal, force, clock=clock)
    if not d.run:
        log.info("star calibration skipped: %s", d.reason)
        return {"attempted": False, "reason": d.reason}
    assert node is not None and d.observer is not None
    log.info("star calibration: requesting %d frames from the node", STACK_FRAMES)
    got = stacker(paths)
    summary: Dict[str, Any] = {"attempted": True, "t_ns": now}
    if got is None:
        summary.update(ok=False, reason="no frames from the node")
    else:
        image, meta = got
        try:
            result = starcal.calibrate(image, int(meta["t_ns"]), starcal.Observer(*d.observer),
                                       make_prior(starcal, node))
        except Exception as exc:  # noqa: BLE001 - a crashing solver must not crash the service
            log.exception("star calibration raised")
            summary.update(ok=False, reason=f"solver error: {exc}")
        else:
            summary.update(ok=bool(getattr(result, "ok", False)), reason=str(getattr(result, "reason", "")),
                           stars_matched=getattr(result, "stars_matched", None),
                           rms_deg=getattr(result, "rms_deg", None))
            if summary["ok"]:
                data = from_result(result, int(meta["width"]), int(meta["height"]))
                data.setdefault("t_ns", int(meta["t_ns"]))
                try:
                    save_calibration(paths.calibration, data)
                    log.info("calibration written: yaw %.3f pitch %.3f roll %.3f, %s stars, rms %s deg",
                             data["yaw_deg"], data["pitch_deg"], data["roll_deg"], data.get("stars_matched"),
                             data.get("rms_deg"))
                except ValueError as exc:
                    summary.update(ok=False, reason=f"result rejected: {exc}")
    if not summary.get("ok"):
        log.info("star calibration did not succeed: %s", summary.get("reason"))
    try:
        write_json(paths.state / "starcal-last.json", summary)
    except OSError as exc:
        log.warning("cannot write starcal-last.json: %s", exc)
    return summary
