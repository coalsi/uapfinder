"""Synthetic sky: camera nodes and moving objects with real geometry.

Used by the test-suite and the ingest benchmark to produce the packets a real
network would send: each node reports an object only while it is inside the
camera's field of view, above a minimum elevation and within detection
range, with Gaussian angular noise, at the node's report rate. Everything
goes through skynet_common (geodesy, camera model, protocol signing), so the
server is exercised with exactly the conventions the edge nodes use.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from skynet_common import geodesy as g
from skynet_common.camera import PinholeCamera, azel_to_pixel
from skynet_common.protocol import derive_node_key, encode_signed


@dataclass
class SimNode:
    node_id: str
    lat: float
    lon: float
    alt_m: float
    yaw_deg: float = 0.0
    pitch_deg: float = 90.0
    roll_deg: float = 0.0
    fov_h_deg: float = 90.0
    fov_v_deg: float = 90.0
    width: int = 1920
    height: int = 1080
    seq: int = 0

    def __post_init__(self) -> None:
        self.camera = PinholeCamera.from_fov(self.width, self.height, self.fov_h_deg, self.fov_v_deg)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def sees(self, az: float, el: float, rng: float, min_el_deg: float, max_range_m: float) -> bool:
        if el < min_el_deg or rng > max_range_m:
            return False
        px = azel_to_pixel(self.camera, az, el, self.yaw_deg, self.pitch_deg, self.roll_deg)
        return px is not None and self.camera.in_image(*px)


@dataclass
class SimObject:
    """Constant-acceleration motion in the local ENU frame of ``origin``."""

    object_id: str
    origin: Tuple[float, float, float]  # lat, lon, h at t0
    velocity_enu: Tuple[float, float, float]
    accel_enu: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    t0_ns: int = 0

    def position(self, t_ns: int) -> Tuple[float, float, float]:
        dt = (t_ns - self.t0_ns) / 1e9
        e = self.velocity_enu[0] * dt + 0.5 * self.accel_enu[0] * dt * dt
        n = self.velocity_enu[1] * dt + 0.5 * self.accel_enu[1] * dt * dt
        u = self.velocity_enu[2] * dt + 0.5 * self.accel_enu[2] * dt * dt
        o = g.geodetic_to_ecef(*self.origin)
        d = g.enu_to_ecef_vector(self.origin[0], self.origin[1], (e, n, u))
        return g.ecef_to_geodetic(o[0] + d[0], o[1] + d[1], o[2] + d[2])


def telemetry_dict(node: SimNode, t_ns: int, az: float, el: float, track_id: Optional[str],
                   seq: Optional[int] = None) -> Dict:
    return {
        "type": "telemetry",
        "node_id": node.node_id,
        "gps_coordinates": {"latitude": node.lat, "longitude": node.lon, "altitude_m": node.alt_m},
        "imu_orientation": {"pitch_deg": node.pitch_deg, "roll_deg": node.roll_deg, "yaw_deg": node.yaw_deg},
        "timestamp_ns": t_ns,
        "vector_target": {"azimuth_deg": round(az % 360.0, 6), "elevation_deg": round(el, 6)},
        "pixel_metrics": {"angular_velocity_deg_s": 1.0, "relative_intensity": 0.8},
        "seq": node.next_seq() if seq is None else seq,
        "track_id": track_id,
        "anomaly_score": 0.5,
    }


def heartbeat_dict(node: SimNode, t_ns: int) -> Dict:
    return {
        "type": "heartbeat",
        "node_id": node.node_id,
        "timestamp_ns": t_ns,
        "seq": node.next_seq(),
        "gps_coordinates": {"latitude": node.lat, "longitude": node.lon, "altitude_m": node.alt_m},
        "imu_orientation": {"pitch_deg": node.pitch_deg, "roll_deg": node.roll_deg, "yaw_deg": node.yaw_deg},
        "camera": {"width": node.width, "height": node.height, "fps": 30.0, "fov_h_deg": node.fov_h_deg,
                   "fov_v_deg": node.fov_v_deg},
        "status": {"fps": 29.9, "tracks_active": 1},
        "software_version": "sim",
    }


@dataclass
class SimNetwork:
    master_key: bytes
    nodes: List[SimNode] = field(default_factory=list)
    objects: List[SimObject] = field(default_factory=list)
    angular_noise_deg: float = 0.03
    min_elevation_deg: float = 5.0
    max_range_m: float = 120_000.0
    seed: int = 1

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self._keys: Dict[str, bytes] = {}

    def key(self, node_id: str) -> bytes:
        k = self._keys.get(node_id)
        if k is None:
            k = self._keys[node_id] = derive_node_key(self.master_key, node_id)
        return k

    def sign(self, msg: Dict) -> bytes:
        return encode_signed(msg, self.key(msg["node_id"]))

    def observe(self, node: SimNode, obj: SimObject, t_ns: int, noise: bool = True
                ) -> Optional[Tuple[float, float]]:
        lat, lon, h = obj.position(t_ns)
        az, el, rng = g.look_angles(node.lat, node.lon, node.alt_m, lat, lon, h)
        if not node.sees(az, el, rng, self.min_elevation_deg, self.max_range_m):
            return None
        if noise and self.angular_noise_deg > 0:
            az += self.rng.gauss(0.0, self.angular_noise_deg)
            el += self.rng.gauss(0.0, self.angular_noise_deg)
        return az, el

    def frame(self, t_ns: int, jitter_ns: int = 0) -> Iterator[Tuple[SimNode, Dict]]:
        """Telemetry of every visible (node, object) pair at one frame time.

        ``jitter_ns`` offsets each node's capture time by up to that much, as
        unsynchronised cameras would.
        """
        for node in self.nodes:
            t = t_ns + (self.rng.randrange(-jitter_ns, jitter_ns + 1) if jitter_ns else 0)
            for obj in self.objects:
                seen = self.observe(node, obj, t)
                if seen is not None:
                    yield node, telemetry_dict(node, t, seen[0], seen[1], f"{obj.object_id}@{node.node_id}"[:64])


def cluster(prefix: str, center: Tuple[float, float], count: int, radius_m: float, rng: random.Random,
            alt_m: float = 350.0) -> List[SimNode]:
    """Nodes scattered around a centre, cameras tilted towards the sky above it."""
    nodes = []
    for i in range(count):
        r = radius_m * math.sqrt(rng.random())
        theta = rng.uniform(0.0, 2.0 * math.pi)
        dlat = (r * math.cos(theta)) / 111_320.0
        dlon = (r * math.sin(theta)) / (111_320.0 * math.cos(math.radians(center[0])))
        lat, lon = center[0] + dlat, center[1] + dlon
        # Point roughly at the sky above the cluster centre, plus some spread.
        az_to_center = math.degrees(math.atan2(-dlon * math.cos(math.radians(center[0])), -dlat)) % 360.0
        nodes.append(SimNode(
            node_id=f"{prefix}-{i:03d}", lat=lat, lon=lon, alt_m=alt_m + rng.uniform(-50, 50),
            yaw_deg=(az_to_center + rng.uniform(-30, 30)) % 360.0, pitch_deg=rng.uniform(35.0, 70.0),
            roll_deg=0.0, fov_h_deg=90.0, fov_v_deg=58.7,
        ))
    return nodes
