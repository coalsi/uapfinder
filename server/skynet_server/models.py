"""Server-side records shared by the pipeline, the state backends and the API.

Records are plain slotted dataclasses rather than Pydantic models: thousands
are created per second on the ingest path. The Redis backend stores track
state in compact positional form (``TrackState.to_row``/``from_row``) and
detections as packed binary (``state.redis.pack_detection``) to keep payloads
small.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from skynet_common.geodesy import Vec3, ecef_to_enu_vector, enu_to_azel

PUBLIC_ALT_STEP_M = 10.0


def public_position(lat: float, lon: float, alt_m: float, decimals: int) -> Tuple[float, float, float]:
    """A node position as it may be published: latitude and longitude rounded
    to ``decimals`` places (2 = about 1 km) and the height to 10 m, because
    a precise height plus a terrain model would narrow the location down
    within the rounded cell again."""
    return round(lat, decimals), round(lon, decimals), round(alt_m / PUBLIC_ALT_STEP_M) * PUBLIC_ALT_STEP_M


@dataclass(slots=True)
class NodeRecord:
    node_id: str
    label: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_m: Optional[float] = None
    yaw_deg: Optional[float] = None
    pitch_deg: Optional[float] = None
    roll_deg: Optional[float] = None
    fov_h_deg: Optional[float] = None
    fov_v_deg: Optional[float] = None
    last_seen_ns: int = 0
    status: Dict[str, Any] = field(default_factory=dict)
    software_version: Optional[str] = None
    registered_ns: Optional[int] = None

    def public(self, decimals: int, now_ns: int, active_ns: int) -> Dict[str, Any]:
        """The API's Node object, with the position rounded for privacy."""
        lat = lon = alt = None
        if self.lat is not None and self.lon is not None and self.alt_m is not None:
            lat, lon, alt = public_position(self.lat, self.lon, self.alt_m, decimals)
        return {
            "node_id": self.node_id,
            "label": self.label,
            "lat": lat,
            "lon": lon,
            "alt_m": alt,
            "yaw_deg": self.yaw_deg,
            "pitch_deg": self.pitch_deg,
            "roll_deg": self.roll_deg,
            "fov_h_deg": self.fov_h_deg,
            "fov_v_deg": self.fov_v_deg,
            "last_seen_ns": self.last_seen_ns or None,
            "online": bool(self.last_seen_ns) and now_ns - self.last_seen_ns <= active_ns,
            "status": self.status,
            "software_version": self.software_version,
        }


@dataclass(slots=True)
class NodeUpdate:
    """Node state carried by one validated message."""

    node_id: str
    t_ns: int
    lat: float
    lon: float
    alt_m: float
    yaw_deg: float
    pitch_deg: float
    roll_deg: float
    fov_h_deg: Optional[float] = None  # heartbeat only
    fov_v_deg: Optional[float] = None
    status: Optional[Dict[str, Any]] = None
    software_version: Optional[str] = None

    @property
    def is_heartbeat(self) -> bool:
        return self.fov_h_deg is not None


@dataclass(slots=True)
class Detection:
    """One line-of-sight observation, with its ECEF ray precomputed once at ingest."""

    node_id: str
    t_ns: int
    seq: int
    track_id: Optional[str]
    lat: float
    lon: float
    alt_m: float
    az_deg: float
    el_deg: float
    origin: Vec3
    direction: Vec3


@dataclass(slots=True)
class RayObs:
    """A ray contributing to a triangulation (possibly interpolated in time).

    For interpolated rays ``az_deg``/``el_deg`` start as None and are derived
    from ``direction`` on first use by ``azel()``: most rays only take part
    in matching and never need them.
    """

    node_id: str
    seq: int  # seq of the (nearest) sample used
    track_id: Optional[str]
    lat: float
    lon: float
    alt_m: float
    az_deg: Optional[float]
    el_deg: Optional[float]
    origin: Vec3
    direction: Vec3
    interpolated: bool = False

    def azel(self) -> Tuple[float, float]:
        if self.az_deg is None or self.el_deg is None:
            self.az_deg, self.el_deg = enu_to_azel(*ecef_to_enu_vector(self.lat, self.lon, self.direction))
        return self.az_deg, self.el_deg


@dataclass(slots=True)
class Match:
    """A confirmed stereo (or multi-node) triangulation of one packet."""

    t_ns: int
    rays: List[RayObs]
    point: Vec3
    lat: float
    lon: float
    alt_m: float
    sigma_m: float
    error_margin_m: float
    parallax_deg: float
    pair_keys: List[str]
    recv_perf_ns: int = 0  # receipt time of the packet that produced it
    link_keys: Optional[List[str]] = None  # cached by TrackManager.link_keys


@dataclass(slots=True)
class TrackState:
    """Live state of a 3D track.

    ``recent`` holds (t_ns, x, y, z, sigma_m) ECEF samples inside the short
    kinematics window. ``history`` is a decimated record of the last ~30 s:
    one [slot, n, sum_t_s, sum_x, sum_y, sum_z, sum_var] entry per second
    (times relative to ``created_ns``), so long-baseline fits (ballistic,
    stationary) stay cheap and the record stays small. The full point
    history lives in a separate capped list.
    """

    track_id: str
    created_ns: int
    first_ns: int
    last_ns: int
    node_ids: List[str] = field(default_factory=list)
    point_count: int = 0
    last_point: Optional[Dict[str, Any]] = None
    recent: List[Tuple[int, float, float, float, float]] = field(default_factory=list)
    history: List[List[float]] = field(default_factory=list)
    speed_mps: Optional[float] = None
    max_speed_mps: float = 0.0
    max_accel_mps2: float = 0.0
    classification: str = "unknown"
    flags: List[str] = field(default_factory=list)
    clip_nodes: List[str] = field(default_factory=list)
    last_event_ns: int = 0
    last_fit_ns: int = 0
    last_long_fit_ns: int = 0

    def public(self) -> Dict[str, Any]:
        return {
            "track_id": self.track_id,
            "first_ns": self.first_ns,
            "last_ns": self.last_ns,
            "node_ids": list(self.node_ids),
            "point_count": self.point_count,
            "last_point": self.last_point,
            "speed_mps": None if self.speed_mps is None else round(self.speed_mps, 2),
            "max_speed_mps": round(self.max_speed_mps, 2),
            "max_accel_mps2": round(self.max_accel_mps2, 2),
            "classification": self.classification,
            "flags": list(self.flags),
        }

    def to_row(self) -> list:
        return [self.track_id, self.created_ns, self.first_ns, self.last_ns, self.node_ids, self.point_count,
                self.last_point, self.recent, self.history, self.speed_mps, self.max_speed_mps,
                self.max_accel_mps2, self.classification, self.flags, self.clip_nodes, self.last_event_ns,
                self.last_fit_ns, self.last_long_fit_ns]

    @classmethod
    def from_row(cls, r: list) -> "TrackState":
        return cls(r[0], r[1], r[2], r[3], r[4], r[5], r[6], [tuple(p) for p in r[7]], r[8], r[9], r[10], r[11],
                   r[12], r[13], r[14], r[15], r[16], r[17])


def track_point(t_ns: int, lat: float, lon: float, alt_m: float, sigma_m: float, error_margin_m: float,
                node_ids: List[str]) -> Dict[str, Any]:
    return {
        "t_ns": t_ns,
        "lat": round(lat, 7),
        "lon": round(lon, 7),
        "alt_m": round(alt_m, 2),
        "sigma_m": round(sigma_m, 2),
        "error_margin_m": round(error_margin_m, 2),
        "node_ids": node_ids,
    }


@dataclass(slots=True)
class ClipRecord:
    request_id: str
    event_id: str
    track_id: str
    node_id: str
    t_start_ns: int
    t_end_ns: int
    issued_ns: int
    status: str = "requested"  # requested | uploaded | failed
    bytes: Optional[int] = None
    sha256: Optional[str] = None
    content_type: Optional[str] = None
    frames: Optional[int] = None
    storage_key: Optional[str] = None
    uploaded_ns: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {name: getattr(self, name) for name in self.__slots__}  # type: ignore[attr-defined]

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ClipRecord":
        return cls(**d)


@dataclass(slots=True)
class Commit:
    """Everything one processed batch writes back to the state backend."""

    tracks: Dict[str, TrackState] = field(default_factory=dict)
    new_points: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    links: Dict[str, str] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    clips: List[ClipRecord] = field(default_factory=list)
    commands: Dict[str, List[str]] = field(default_factory=dict)
    # UDP return addresses to share with other instances: node -> ((host, port), seen_ns)
    udp_addrs: Dict[str, Tuple[Tuple[str, int], int]] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.tracks or self.links or self.events or self.clips or self.commands or self.udp_addrs)
