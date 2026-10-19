"""Telemetry and heartbeat message bodies (docs/PROTOCOL.md).

Only plain dicts are built here; ``uplink`` assigns ``seq``, signs with
``skynet_common.protocol.encode_signed`` and checks the 1200-byte datagram
limit. Values are rounded to what the measurement supports (0.0001 deg is
0.4 m at 2 km and far below the ~0.01 deg centroid noise) to keep datagrams
small.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from skynet_common.protocol import MSG_HEARTBEAT, MSG_TELEMETRY

from . import __version__
from .geometry import Attitude, CameraModel
from .pipeline import Report
from .sensors.gps import Fix


def telemetry(node_id: str, fix: Fix, att: Attitude, r: Report) -> Dict[str, Any]:
    return {
        "type": MSG_TELEMETRY,
        "node_id": node_id,
        "gps_coordinates": fix.as_message(),
        "imu_orientation": att.as_message(),
        "timestamp_ns": int(r.t_ns),
        "vector_target": {"azimuth_deg": round(r.az_deg % 360.0, 5), "elevation_deg": round(r.el_deg, 5)},
        "pixel_metrics": {"angular_velocity_deg_s": round(max(0.0, r.angular_velocity_deg_s), 4),
                          "relative_intensity": round(r.relative_intensity, 4)},
        "track_id": r.track_id,
        "anomaly_score": round(min(1.0, max(0.0, r.anomaly_score)), 3),
    }


def heartbeat(node_id: str, t_ns: int, fix: Fix, att: Attitude, cam: CameraModel, fps: float,
              status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "type": MSG_HEARTBEAT,
        "node_id": node_id,
        "timestamp_ns": int(t_ns),
        "gps_coordinates": fix.as_message(),
        "imu_orientation": att.as_message(),
        "camera": {"width": cam.width, "height": cam.height, "fps": round(float(fps), 3),
                   "fov_h_deg": round(cam.fov_h_deg, 3), "fov_v_deg": round(cam.fov_v_deg, 3)},
        "status": dict(status or {}),
        "software_version": __version__,
    }
