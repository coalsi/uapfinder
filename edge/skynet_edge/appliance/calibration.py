"""``/var/lib/skynet/calibration.json``: the pointing and lens model from star calibration.

Format (written atomically by ``starcal_job`` from a successful
``skynet_edge.starcal.calibrate`` result)::

    {"version": 1, "ok": true, "source": "starcal", "t_ns": 1790000000000000000,
     "width": 1920, "height": 1080,
     "yaw_deg": 12.3, "pitch_deg": 71.0, "roll_deg": -1.2,
     "fx": 960.0, "fy": 960.0, "cx": 959.5, "cy": 539.5, "k1": -0.21, "k2": 0.04,
     "stars_matched": 57, "rms_deg": 0.03}

``autoconfig`` turns it into ``camera.intrinsics`` and a static attitude.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .util import read_json, write_json

REQUIRED = ("t_ns", "width", "height", "yaw_deg", "pitch_deg", "roll_deg")
FIELDS = ("t_ns", "width", "height", "yaw_deg", "pitch_deg", "roll_deg", "fx", "fy", "cx", "cy", "k1", "k2",
          "stars_matched", "rms_deg")


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_calibration(data: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict) or data.get("ok") is False:
        return None
    if not all(_finite(data.get(k)) for k in REQUIRED):
        return None
    if not (-90 <= data["pitch_deg"] <= 90 and -180 <= data["roll_deg"] <= 180):
        return None
    if data.get("fx") is not None and not (_finite(data["fx"]) and data["fx"] > 0
                                           and all(_finite(data.get(k)) for k in ("cx", "cy"))):
        return None
    return data


def load_calibration(path: Union[str, Path]) -> Optional[Dict[str, Any]]:
    return validate_calibration(read_json(path))


def calibration_age_days(calib: Optional[Dict[str, Any]], now_ns: Optional[int] = None) -> Optional[float]:
    if not calib:
        return None
    now = time.time_ns() if now_ns is None else now_ns
    return round((now - int(calib["t_ns"])) / 86400e9, 2)


def from_result(result: Any, width: int, height: int) -> Dict[str, Any]:
    """calibration.json content from a ``StarCalResult`` (attribute access)."""
    out: Dict[str, Any] = {"version": 1, "ok": True, "source": "starcal", "width": int(width), "height": int(height)}
    for k in FIELDS:
        if k in ("width", "height"):
            continue
        v = getattr(result, k, None)
        if v is None:
            continue
        out[k] = int(v) if k in ("t_ns", "stars_matched") else float(v)
    return out


def save_calibration(path: Union[str, Path], data: Dict[str, Any]) -> None:
    if validate_calibration(data) is None:
        raise ValueError("refusing to write an invalid calibration")
    write_json(path, data, mode=0o644)
