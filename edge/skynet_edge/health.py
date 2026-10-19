"""Node health: memory, temperatures, clock discipline and systemd notification.

* Memory: process RSS / peak (``VmRSS`` / ``VmHWM`` from /proc/self/status)
  and system ``MemAvailable``. On a Jetson the 8 GB DRAM is shared by the
  CPU and GPU (unified memory), so these numbers include what CUDA uses.
* Temperatures: every ``/sys/class/thermal/thermal_zone*`` (on Orin the zones
  are named ``cpu-thermal``, ``gpu-thermal``, ``soc0-thermal``, ``tj-thermal``
  ...). Values are millidegrees Celsius.
* Clock: ``chronyc -c tracking`` (CSV): field 2 is the reference name (e.g.
  ``PPS``), field 5 the current system time correction in seconds. Telemetry
  timestamps are only as good as this offset.
* systemd: ``sd_notify`` implemented directly on ``$NOTIFY_SOCKET`` (a unix
  datagram socket; a leading ``@`` means the abstract namespace) so there is
  no libsystemd dependency. ``READY=1`` once frames flow, ``WATCHDOG=1``
  while frames keep being processed, ``STOPPING=1`` on shutdown.
"""

from __future__ import annotations

import glob
import logging
import os
import socket
import subprocess
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)


def _status_kb(field: str, path: str = "/proc/self/status") -> Optional[int]:
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def process_memory_mb() -> Dict[str, float]:
    """Resident and peak resident memory of this process, MiB."""
    rss, hwm = _status_kb("VmRSS"), _status_kb("VmHWM")
    return {"rss_mb": round((rss or 0) / 1024.0, 1), "peak_rss_mb": round((hwm or 0) / 1024.0, 1)}


def system_memory_mb() -> Dict[str, float]:
    total = _status_kb("MemTotal", "/proc/meminfo")
    avail = _status_kb("MemAvailable", "/proc/meminfo")
    return {"total_mb": round((total or 0) / 1024.0, 1), "available_mb": round((avail or 0) / 1024.0, 1)}


def thermal_zones_c(root: str = "/sys/class/thermal") -> Dict[str, float]:
    """Zone name -> temperature in deg C, for every readable thermal zone."""
    out: Dict[str, float] = {}
    for zone in sorted(glob.glob(os.path.join(root, "thermal_zone*"))):
        try:
            name = Path(zone, "type").read_text().strip()
            milli = int(Path(zone, "temp").read_text().strip())
        except (OSError, ValueError):
            continue
        out[name] = round(milli / 1000.0, 1)
    return out


def headline_temperatures(zones: Dict[str, float]) -> Dict[str, float]:
    """``gpu_temp_c`` / ``cpu_temp_c`` / ``max_temp_c`` from the zone list (dashboard keys)."""
    out: Dict[str, float] = {}
    for key, needle in (("gpu_temp_c", "gpu"), ("cpu_temp_c", "cpu")):
        vals = [v for k, v in zones.items() if needle in k.lower()]
        if vals:
            out[key] = max(vals)
    if zones:
        out["max_temp_c"] = max(zones.values())
    return out


def parse_chrony_tracking_csv(line: str) -> Optional[Dict[str, object]]:
    fields = line.strip().split(",")
    if len(fields) < 14:
        return None
    try:
        return {"clock_ref": fields[1], "clock_stratum": int(fields[2]),
                "clock_offset_ms": round(float(fields[4]) * 1e3, 4), "clock_leap": fields[13]}
    except ValueError:
        return None


def chrony_tracking(timeout_s: float = 2.0) -> Optional[Dict[str, object]]:
    try:
        proc = subprocess.run(["chronyc", "-c", "tracking"], capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_chrony_tracking_csv(proc.stdout) if proc.returncode == 0 else None


class SdNotifier:
    """Minimal sd_notify(3) client; does nothing when not started by systemd."""

    def __init__(self, env: Optional[Dict[str, str]] = None):
        env = dict(os.environ) if env is None else env
        addr = env.get("NOTIFY_SOCKET")
        self._addr: Optional[str] = None
        if addr:
            self._addr = "\0" + addr[1:] if addr.startswith("@") else addr
        usec = env.get("WATCHDOG_USEC")
        pid = env.get("WATCHDOG_PID")
        self.watchdog_s: Optional[float] = None
        if usec and (not pid or pid == str(os.getpid())):
            try:
                self.watchdog_s = int(usec) / 1e6
            except ValueError:
                pass

    @property
    def enabled(self) -> bool:
        return self._addr is not None

    def notify(self, state: str) -> bool:
        if self._addr is None:
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.sendto(state.encode("utf-8"), self._addr)
            return True
        except OSError as exc:
            log.debug("sd_notify failed: %s", exc)
            return False

    def ready(self, status: str = "") -> bool:
        return self.notify("READY=1" + (f"\nSTATUS={status}" if status else ""))

    def watchdog(self) -> bool:
        return self.notify("WATCHDOG=1")

    def status(self, text: str) -> bool:
        return self.notify(f"STATUS={text}")

    def stopping(self) -> bool:
        return self.notify("STOPPING=1")
