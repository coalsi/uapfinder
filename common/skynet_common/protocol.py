"""SKY-NET wire protocol: message models, validation and HMAC signing.

Every message is a single compact JSON object. Edge nodes send ``telemetry``
and ``heartbeat`` messages; the server sends ``clip_request`` messages back.
The same bytes travel either as one UDP datagram or as one line of an NDJSON
HTTPS batch (``POST /api/v1/ingest``), so a node can switch transports without
re-signing anything.

Signing (see docs/PROTOCOL.md): the sender serialises the message without a
``sig`` member, computes ``HMAC-SHA256(node_key, body)`` and appends it as the
final member, producing ``{...,"sig":"<64 lowercase hex>"}``. The receiver
recovers the signed bytes by stripping that fixed-size suffix, so no JSON
canonicalisation is needed and verification runs on the raw received bytes.

Per-node keys are derived from one server master key, so the server never
stores per-node secrets: ``node_key = HMAC-SHA256(master_key, "skynet-node-key:" + node_id)``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Tuple

PROTOCOL_VERSION = 1

MSG_TELEMETRY = "telemetry"
MSG_HEARTBEAT = "heartbeat"
MSG_CLIP_REQUEST = "clip_request"
MESSAGE_TYPES = (MSG_TELEMETRY, MSG_HEARTBEAT, MSG_CLIP_REQUEST)

# Keep datagrams below the IPv6 minimum MTU (1280) minus headers so they are
# never fragmented. A typical telemetry message is ~450 bytes.
MAX_DATAGRAM_BYTES = 1200
MAX_BATCH_LINES = 2000

# Valid timestamps are nanoseconds since the Unix epoch between 2001-09-09
# and 2096-10-02. This rejects the common mistake of sending microseconds or
# milliseconds in the timestamp_ns field.
MIN_TIMESTAMP_NS = 1_000_000_000_000_000_000
MAX_TIMESTAMP_NS = 4_000_000_000_000_000_000

_SIG_PREFIX = b',"sig":"'
_SIG_HEX_LEN = 64
_SIG_SUFFIX_LEN = len(_SIG_PREFIX) + _SIG_HEX_LEN + 2  # + closing '"}'
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_HEX_RE = re.compile(rb"^[0-9a-f]{64}$")


class ProtocolError(ValueError):
    """A message failed parsing, validation or signature checks."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# --- keys and signatures ---------------------------------------------------


def derive_node_key(master_key: bytes, node_id: str) -> bytes:
    """Derive a node's 32-byte HMAC key from the server master key."""
    return hmac.new(master_key, b"skynet-node-key:" + node_id.encode("utf-8"), hashlib.sha256).digest()


def dumps_compact(obj: Dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sign_bytes(body: bytes, key: bytes) -> bytes:
    """Append the HMAC of a compact JSON object as its final ``sig`` member."""
    if len(body) < 3 or not body.startswith(b"{") or not body.endswith(b"}"):
        raise ValueError("body must be a non-empty compact JSON object")
    digest = hmac.new(key, body, hashlib.sha256).hexdigest().encode("ascii")
    return body[:-1] + _SIG_PREFIX + digest + b'"}'


def encode_signed(obj: Dict[str, Any], key: Optional[bytes]) -> bytes:
    """Serialise a message dict, signing it when a key is given."""
    if "sig" in obj:
        obj = {k: v for k, v in obj.items() if k != "sig"}
    body = dumps_compact(obj)
    return sign_bytes(body, key) if key is not None else body


def split_signature(data: bytes) -> Tuple[bytes, Optional[bytes]]:
    """Split raw message bytes into (signed_body, sig_hex). sig_hex is None if unsigned."""
    data = data.strip()
    if len(data) > _SIG_SUFFIX_LEN and data.endswith(b'"}'):
        start = len(data) - _SIG_SUFFIX_LEN
        if data[start : start + len(_SIG_PREFIX)] == _SIG_PREFIX:
            sig = data[start + len(_SIG_PREFIX) : -2]
            if _HEX_RE.match(sig):
                return data[:start] + b"}", sig
    return data, None


def verify_signature(data: bytes, key: bytes) -> bool:
    body, sig = split_signature(data)
    if sig is None:
        return False
    expected = hmac.new(key, body, hashlib.sha256).hexdigest().encode("ascii")
    return hmac.compare_digest(expected, sig)


def sign_http_request(key: bytes, method: str, path: str, timestamp_ns: int, body: bytes = b"") -> str:
    """Signature for node-authenticated HTTP requests (X-Skynet-Signature header)."""
    msg = b"\n".join(
        [method.upper().encode(), path.encode(), str(timestamp_ns).encode(), hashlib.sha256(body).hexdigest().encode()]
    )
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_http_request(key: bytes, method: str, path: str, timestamp_ns: int, body: bytes, signature: str) -> bool:
    expected = sign_http_request(key, method, path, timestamp_ns, body)
    return hmac.compare_digest(expected, signature)


# --- message models --------------------------------------------------------


def _num(d: Dict[str, Any], key: str, lo: float, hi: float) -> float:
    v = d.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ProtocolError("invalid_field", f"{key} must be a number")
    v = float(v)
    if not (lo <= v <= hi):
        raise ProtocolError("out_of_range", f"{key}={v} not in [{lo}, {hi}]")
    return v


def _obj(d: Dict[str, Any], key: str) -> Dict[str, Any]:
    v = d.get(key)
    if not isinstance(v, dict):
        raise ProtocolError("invalid_field", f"{key} must be an object")
    return v


def _int(d: Dict[str, Any], key: str, lo: int, hi: int, default: Optional[int] = None) -> int:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ProtocolError("invalid_field", f"{key} must be an integer")
    if not (lo <= v <= hi):
        raise ProtocolError("out_of_range", f"{key}={v} not in [{lo}, {hi}]")
    return v


def _node_id(d: Dict[str, Any]) -> str:
    v = d.get("node_id")
    if not isinstance(v, str) or not _NODE_ID_RE.match(v):
        raise ProtocolError("invalid_field", "node_id must be 1-64 chars of [A-Za-z0-9_.:-]")
    return v


def _timestamp(d: Dict[str, Any], key: str = "timestamp_ns") -> int:
    v = d.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ProtocolError("invalid_field", f"{key} must be an integer number of nanoseconds")
    if not (MIN_TIMESTAMP_NS <= v <= MAX_TIMESTAMP_NS):
        raise ProtocolError(
            "bad_timestamp", f"{key}={v} is not nanoseconds since the Unix epoch (expected a 19-digit value)"
        )
    return v


@dataclass
class GpsFix:
    latitude: float
    longitude: float
    altitude_m: float  # height above the WGS84 ellipsoid

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GpsFix":
        return cls(
            latitude=_num(d, "latitude", -90.0, 90.0),
            longitude=_num(d, "longitude", -180.0, 180.0),
            altitude_m=_num(d, "altitude_m", -1000.0, 12000.0),
        )


@dataclass
class Orientation:
    """Camera optical-axis attitude: yaw = azimuth (deg from true north),
    pitch = elevation above the horizon, roll = right-side-down rotation
    about the optical axis."""

    pitch_deg: float
    roll_deg: float
    yaw_deg: float

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Orientation":
        return cls(
            pitch_deg=_num(d, "pitch_deg", -90.0, 90.0),
            roll_deg=_num(d, "roll_deg", -180.0, 180.0),
            yaw_deg=_num(d, "yaw_deg", -360.0, 360.0) % 360.0,
        )


@dataclass
class VectorTarget:
    azimuth_deg: float
    elevation_deg: float

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "VectorTarget":
        return cls(
            azimuth_deg=_num(d, "azimuth_deg", -360.0, 360.0) % 360.0,
            elevation_deg=_num(d, "elevation_deg", -90.0, 90.0),
        )


@dataclass
class PixelMetrics:
    angular_velocity_deg_s: float
    relative_intensity: float

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PixelMetrics":
        return cls(
            angular_velocity_deg_s=_num(d, "angular_velocity_deg_s", 0.0, 1.0e5),
            relative_intensity=_num(d, "relative_intensity", 0.0, 1.0),
        )


@dataclass
class Telemetry:
    """A line-of-sight observation of an unidentified moving object.

    Fields up to ``pixel_metrics`` are exactly the blueprint schema. The rest
    are SKY-NET extensions: ``seq`` (per-node counter for replay detection),
    ``track_id`` (the node's local track identifier, so successive packets for
    one object can be linked) and ``anomaly_score`` (0..1 edge-side kinematic
    anomaly score).
    """

    node_id: str
    gps_coordinates: GpsFix
    imu_orientation: Orientation
    timestamp_ns: int
    vector_target: VectorTarget
    pixel_metrics: PixelMetrics
    seq: int = 0
    track_id: Optional[str] = None
    anomaly_score: Optional[float] = None
    type: str = MSG_TELEMETRY

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Telemetry":
        if d.get("type", MSG_TELEMETRY) != MSG_TELEMETRY:
            raise ProtocolError("wrong_type", str(d.get("type")))
        track_id = d.get("track_id")
        if track_id is not None and (not isinstance(track_id, str) or len(track_id) > 64):
            raise ProtocolError("invalid_field", "track_id must be a string of at most 64 chars")
        score = d.get("anomaly_score")
        return cls(
            node_id=_node_id(d),
            gps_coordinates=GpsFix.from_dict(_obj(d, "gps_coordinates")),
            imu_orientation=Orientation.from_dict(_obj(d, "imu_orientation")),
            timestamp_ns=_timestamp(d),
            vector_target=VectorTarget.from_dict(_obj(d, "vector_target")),
            pixel_metrics=PixelMetrics.from_dict(_obj(d, "pixel_metrics")),
            seq=_int(d, "seq", 0, 2**63 - 1, default=0),
            track_id=track_id,
            anomaly_score=None if score is None else _num(d, "anomaly_score", 0.0, 1.0),
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class CameraInfo:
    width: int
    height: int
    fps: float
    fov_h_deg: float
    fov_v_deg: float

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CameraInfo":
        return cls(
            width=_int(d, "width", 1, 100_000),
            height=_int(d, "height", 1, 100_000),
            fps=_num(d, "fps", 0.0, 10_000.0),
            fov_h_deg=_num(d, "fov_h_deg", 0.1, 360.0),
            fov_v_deg=_num(d, "fov_v_deg", 0.1, 180.0),
        )


@dataclass
class Heartbeat:
    """Periodic node status. Keeps the node in the active spatial index,
    refreshes its UDP return address (NAT keep-alive) and carries the camera
    geometry the dashboard needs to draw viewing frustums."""

    node_id: str
    timestamp_ns: int
    gps_coordinates: GpsFix
    imu_orientation: Orientation
    camera: CameraInfo
    seq: int = 0
    status: Dict[str, Any] = field(default_factory=dict)
    software_version: str = ""
    type: str = MSG_HEARTBEAT

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Heartbeat":
        if d.get("type") != MSG_HEARTBEAT:
            raise ProtocolError("wrong_type", str(d.get("type")))
        status = d.get("status", {})
        if not isinstance(status, dict):
            raise ProtocolError("invalid_field", "status must be an object")
        version = d.get("software_version", "")
        if not isinstance(version, str) or len(version) > 64:
            raise ProtocolError("invalid_field", "software_version must be a short string")
        return cls(
            node_id=_node_id(d),
            timestamp_ns=_timestamp(d),
            gps_coordinates=GpsFix.from_dict(_obj(d, "gps_coordinates")),
            imu_orientation=Orientation.from_dict(_obj(d, "imu_orientation")),
            camera=CameraInfo.from_dict(_obj(d, "camera")),
            seq=_int(d, "seq", 0, 2**63 - 1, default=0),
            status=status,
            software_version=version,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ClipRequest:
    """Server -> node back-signal asking for the HD video around a match.

    Signed with the destination node's key so a node only uploads video
    when the real server asks for it. ``upload_url`` is a server endpoint,
    not the storage location: the node POSTs a signed HTTP request to it and
    receives the actual upload target (a local server path or a presigned
    object-storage URL). That keeps this message small enough for one UDP
    datagram even when presigned URLs are long.
    """

    request_id: str
    event_id: str
    node_id: str
    t_start_ns: int
    t_end_ns: int
    upload_url: str
    issued_ns: int
    type: str = MSG_CLIP_REQUEST

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ClipRequest":
        if d.get("type") != MSG_CLIP_REQUEST:
            raise ProtocolError("wrong_type", str(d.get("type")))
        for key in ("request_id", "event_id", "upload_url"):
            if not isinstance(d.get(key), str) or not d[key] or len(d[key]) > 512:
                raise ProtocolError("invalid_field", f"{key} must be a non-empty string of at most 512 chars")
        start, end = _timestamp(d, "t_start_ns"), _timestamp(d, "t_end_ns")
        if end < start:
            raise ProtocolError("invalid_field", "t_end_ns precedes t_start_ns")
        return cls(
            request_id=d["request_id"],
            event_id=d["event_id"],
            node_id=_node_id(d),
            t_start_ns=start,
            t_end_ns=end,
            upload_url=d["upload_url"],
            issued_ns=_timestamp(d, "issued_ns"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_MODELS = {MSG_TELEMETRY: Telemetry, MSG_HEARTBEAT: Heartbeat, MSG_CLIP_REQUEST: ClipRequest}


def parse_json(data: bytes) -> Dict[str, Any]:
    if len(data) > MAX_DATAGRAM_BYTES:
        raise ProtocolError("too_large", f"{len(data)} bytes > {MAX_DATAGRAM_BYTES}")
    try:
        obj = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("bad_json", str(exc)) from None
    if not isinstance(obj, dict):
        raise ProtocolError("bad_json", "message must be a JSON object")
    return obj


def decode_message(data: bytes, master_key: Optional[bytes], require_signature: bool = True):
    """Parse, authenticate and validate one message.

    ``master_key`` is the server master key from which node keys are derived.
    Returns a Telemetry, Heartbeat or ClipRequest instance.
    """
    obj = parse_json(data)
    msg_type = obj.get("type", MSG_TELEMETRY)
    model = _MODELS.get(msg_type)
    if model is None:
        raise ProtocolError("unknown_type", str(msg_type))
    node_id = _node_id(obj)
    _, sig = split_signature(data)
    if sig is None:
        if require_signature:
            raise ProtocolError("unsigned")
    elif master_key is not None:
        if not verify_signature(data, derive_node_key(master_key, node_id)):
            raise ProtocolError("bad_signature")
    elif require_signature:
        raise ProtocolError("no_key", "server has no master key configured")
    return model.from_dict(obj)


def decode_for_node(data: bytes, node_key: bytes):
    """Node-side decode of a server message signed with this node's key.

    Server messages are always signed; an unsigned or mis-signed message is
    rejected before it is validated.
    """
    obj = parse_json(data)
    if not verify_signature(data, node_key):
        raise ProtocolError("bad_signature")
    model = _MODELS.get(obj.get("type"))
    if model is None:
        raise ProtocolError("unknown_type", str(obj.get("type")))
    return model.from_dict(obj)
