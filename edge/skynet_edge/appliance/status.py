"""Everything the status page shows, gathered without touching the camera.

Sources: ``detect.json`` / ``autoconfig.json`` (written by autoconfig at
each node start), ``node.json`` (the running node), gpsd (fix and
satellites), ``chronyc -c tracking`` / ``chronyc -c sources`` (time error,
PPS lock), the file system holding ``/var/lib/skynet``, the calibration
files, the credentials (without secrets) and, when enrolled, the server's
public ``GET /api/v1/nodes?online_only=true`` (does the server hear this
node?). Slow sources are cached.
"""

from __future__ import annotations

import json
import shutil
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import yaml

from .. import __version__
from ..health import parse_chrony_tracking_csv
from .calibration import calibration_age_days, load_calibration
from .credentials import load_credentials
from .paths import Paths
from .util import CmdResult, file_age_s, read_json, read_text, run

CLOCK_WARN_MS = 2.0


class Cache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}

    def get(self, key: str, ttl_s: float, fn: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit is not None and now - hit[0] < ttl_s:
                return hit[1]
        value = fn()
        with self._lock:
            self._data[key] = (now, value)
        return value


def gpsd_snapshot(addr=("127.0.0.1", 2947), timeout_s: float = 1.5) -> Dict[str, Any]:
    """Fix and satellites from gpsd (TPV + SKY), or ``{"gpsd": False}``."""
    try:
        sock = socket.create_connection(addr, timeout=0.5)
    except OSError:
        return {"gpsd": False}
    out: Dict[str, Any] = {"gpsd": True, "mode": 0, "devices": []}
    try:
        sock.settimeout(0.3)
        sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
        buf = b""
        end = time.monotonic() + timeout_s
        have_tpv = have_sky = False
        while time.monotonic() < end and not (have_tpv and have_sky):
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
                if not isinstance(obj, dict):
                    continue
                cls = obj.get("class")
                if cls == "DEVICES":
                    out["devices"] = [d.get("path") for d in obj.get("devices", []) if isinstance(d, dict)]
                elif cls == "TPV":
                    have_tpv = True
                    out["mode"] = obj.get("mode", 0) if isinstance(obj.get("mode"), int) else 0
                    for k in ("lat", "lon", "altHAE", "altMSL", "time"):
                        if k in obj:
                            out[k] = obj[k]
                elif cls == "SKY":
                    sats = obj.get("satellites")
                    if isinstance(sats, list):
                        have_sky = True
                        out["satellites_visible"] = len(sats)
                        out["satellites_used"] = sum(1 for s in sats if isinstance(s, dict) and s.get("used"))
                    elif isinstance(obj.get("uSat"), int):
                        have_sky = True
                        out["satellites_used"] = obj["uSat"]
                        out["satellites_visible"] = obj.get("nSat")
    finally:
        sock.close()
    return out


def parse_chrony_sources_csv(text: str) -> List[Dict[str, Any]]:
    """``chronyc -c sources``: mode, state, name, stratum, poll, reach, last rx, offsets..."""
    out = []
    for line in text.splitlines():
        f = line.split(",")
        if len(f) < 6:
            continue
        out.append({"mode": f[0], "state": f[1], "name": f[2], "reach": f[5]})
    return out


def chrony_status(runner: Callable[..., CmdResult] = run) -> Dict[str, Any]:
    tr = runner(["chronyc", "-c", "tracking"], timeout=2.0)
    if not tr.ok:
        return {"available": False}
    out: Dict[str, Any] = {"available": True}
    parsed = parse_chrony_tracking_csv(tr.stdout)
    if parsed:
        out.update(parsed)
    fields = tr.stdout.strip().split(",")
    if len(fields) >= 12:
        try:
            # Estimated error bound: |offset| + root dispersion + root delay / 2 (chronyc docs).
            offset, delay, disp = abs(float(fields[4])), float(fields[10]), float(fields[11])
            out["error_ms"] = round((offset + disp + delay / 2.0) * 1e3, 3)
        except ValueError:
            pass
    src = runner(["chronyc", "-c", "sources"], timeout=2.0)
    sources = parse_chrony_sources_csv(src.stdout) if src.ok else []
    pps = [s for s in sources if s["mode"] == "#" and s["name"].upper().startswith("PPS")]
    out["pps_configured"] = bool(pps)
    out["pps_locked"] = any(s["state"] in ("*", "+") for s in pps)
    out["sources"] = sources[:12]
    return out


def storage_status(paths: Paths, proc_root: str = "/proc") -> Dict[str, Any]:
    out: Dict[str, Any] = {"path": str(paths.state)}
    try:
        du = shutil.disk_usage(paths.state)
        out.update(total_gb=round(du.total / 1e9, 1), free_gb=round(du.free / 1e9, 1))
    except OSError:
        pass
    best = ("", "")
    for line in (read_text(f"{proc_root}/mounts", 1 << 20) or "").splitlines():
        p = line.split()
        if len(p) >= 2 and str(paths.state).startswith(p[1]) and len(p[1]) > len(best[1]):
            best = (p[0], p[1])
    out["device"] = best[0] or None
    out["nvme"] = best[0].startswith("/dev/nvme")
    return out


def server_status(creds: Any, timeout_s: float = 3.0, session: Any = None) -> Dict[str, Any]:
    base = creds.data.get("https_base_url") if creds else None
    if not base:
        return {"checked": False}
    import requests

    http = session or requests
    try:
        resp = http.get(f"{base}/api/v1/nodes", params={"online_only": "true"}, timeout=timeout_s)
        if resp.status_code != 200:
            return {"checked": True, "reachable": True, "http_status": resp.status_code}
        nodes = resp.json().get("nodes", [])
        seen = any(isinstance(n, dict) and n.get("node_id") == creds.node_id for n in nodes)
        return {"checked": True, "reachable": True, "online": seen}
    except (requests.RequestException, ValueError, AttributeError) as exc:
        return {"checked": True, "reachable": False, "error": type(exc).__name__}


def edge_config_summary(paths: Paths) -> Dict[str, Any]:
    text = read_text(paths.edge_yaml, 1 << 20)
    try:
        cfg = yaml.safe_load(text) if text else {}
    except yaml.YAMLError:
        return {}
    cfg = cfg if isinstance(cfg, dict) else {}
    adsb = cfg.get("adsb") or {}
    url = adsb.get("url") or ""
    host = url.split("/")[2] if url.count("/") >= 2 else None
    return {"adsb_source": adsb.get("source", "none"), "adsb_host": host, "adsb_file": adsb.get("file_path"),
            "gps_source": (cfg.get("gps") or {}).get("source"), "camera_source": (cfg.get("camera") or {}).get("source")}


class StatusCollector:
    def __init__(self, paths: Paths, runner: Callable[..., CmdResult] = run, nm: Any = None,
                 gpsd: Callable[[], Dict[str, Any]] = gpsd_snapshot,
                 server_check: Callable[[Any], Dict[str, Any]] = server_status):
        self.paths = paths
        self._run = runner
        self.nm = nm
        self._gpsd = gpsd
        self._server = server_check
        self.cache = Cache()

    def collect(self) -> Dict[str, Any]:
        p = self.paths
        det = read_json(p.detect_json) or {}
        auto = read_json(p.run / "autoconfig.json") or {}
        node = read_json(p.node_json)
        node_age = file_age_s(p.node_json)
        creds = load_credentials(p.credentials)
        calib = load_calibration(p.calibration)
        last_cal = read_json(p.state / "starcal-last.json")
        gps = self.cache.get("gps", 5.0, self._gpsd)
        chrony = self.cache.get("chrony", 10.0, lambda: chrony_status(self._run))
        storage = self.cache.get("storage", 30.0, lambda: storage_status(p))
        server = self.cache.get("server", 30.0, lambda: self._server(creds)) if creds else {"checked": False}
        network = self.cache.get("network", 5.0, self._network)
        cfgsum = self.cache.get("cfg", 10.0, lambda: edge_config_summary(p))
        node_running = bool(node) and node_age is not None and node_age < 30.0
        st: Dict[str, Any] = {
            "version": __version__,
            "hostname": socket.gethostname(),
            "time_ns": time.time_ns(),
            "enrolled": creds is not None,
            "identity": creds.public() if creds else None,
            "hardware": {k: det.get(k) for k in ("platform", "camera", "gps", "pps", "imu", "adsb", "nvme")},
            "detect_age_s": None if not det else round((time.time_ns() - det.get("t_ns", 0)) / 1e9),
            "autoconfig": auto,
            "node": {"running": node_running, "mode": (node or {}).get("mode"), "age_s": node_age,
                     "attitude": (node or {}).get("attitude"), "attitude_source": (node or {}).get("attitude_source"),
                     "pointing_source": (node or {}).get("pointing_source"),
                     "pointing_sigma_deg": (node or {}).get("pointing_sigma_deg"),
                     "camera": (node or {}).get("camera"), "server": (node or {}).get("server"),
                     "status": (node or {}).get("status")},
            "gps": gps,
            "time": chrony,
            "storage": storage,
            "server": server,
            "network": network,
            "config": cfgsum,
            "calibration": None if calib is None else {
                "age_days": calibration_age_days(calib), "stars_matched": calib.get("stars_matched"),
                "rms_deg": calib.get("rms_deg"), "yaw_deg": calib.get("yaw_deg"), "pitch_deg": calib.get("pitch_deg"),
                "roll_deg": calib.get("roll_deg")},
            "calibration_last_attempt": last_cal if isinstance(last_cal, dict) else None,
        }
        st["warnings"] = warnings(st)
        return st

    def _network(self) -> Dict[str, Any]:
        if self.nm is None:
            return {"manager": None}
        devs = self.nm.devices()
        return {"manager": "networkmanager" if devs else None, "connected": self.nm.is_connected(devs),
                "hotspot": self.nm.hotspot_active(devs),
                "devices": [{"device": d.device, "type": d.type, "state": d.state, "connection": d.connection}
                            for d in devs if d.type in ("ethernet", "wifi")]}


def warnings(st: Dict[str, Any]) -> List[str]:
    w = []
    hw = st.get("hardware") or {}
    if not st.get("enrolled"):
        w.append("Not enrolled yet: enter the claim code from your coordinator.")
    if (hw.get("camera") or {}).get("kind", "none") == "none":
        w.append("No camera found. Check the ribbon cable in CAM0 (contacts facing the board) and restart.")
    gps = st.get("gps") or {}
    if not gps.get("gpsd"):
        w.append("The GPS service (gpsd) is not running.")
    elif (gps.get("mode") or 0) < 2:
        w.append("No GPS fix yet. The GPS needs a clear view of the sky; the first fix can take several minutes.")
    t = st.get("time") or {}
    err = t.get("error_ms")
    if isinstance(err, (int, float)) and err > CLOCK_WARN_MS:
        w.append(f"Time error is {err:.1f} ms (above {CLOCK_WARN_MS:g} ms): fast objects will be placed less "
                 "precisely. A GPS with PPS on pin 7 fixes this.")
    auto = st.get("autoconfig") or {}
    if auto.get("error"):
        w.append(f"Configuration: {auto['error']}")
    if st.get("enrolled") and not st.get("calibration"):
        w.append("Pointing: waiting for a clear night (automatic star calibration).")
    return w


def uptime_s(proc_root: str = "/proc") -> Optional[float]:
    t = read_text(f"{proc_root}/uptime", 128)
    try:
        return float(t.split()[0]) if t else None
    except ValueError:
        return None

