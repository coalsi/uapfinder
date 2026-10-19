"""Edge node configuration: YAML file -> validated, typed dataclasses.

A node runs unattended on a rooftop for months, so configuration mistakes
must fail loudly at start-up rather than surface as silently wrong
az/el vectors later. The loader therefore rejects unknown keys (typos),
checks every value's type and range, and builds plain dataclasses that the
rest of the code can rely on without re-validating.

Only PyYAML is used (``yaml.safe_load``); there is no schema library
dependency, which keeps the Jetson install small.
"""

from __future__ import annotations

import dataclasses
import re
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

import yaml

_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_HEX_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# Default capture pipeline for a CSI sensor on a Jetson. nvarguscamerasrc
# delivers NV12 in NVMM (GPU) memory; nvvidconv copies it to system memory as
# planar I420, which OpenCV's appsink reader returns as one (H*3/2, W) uint8
# plane -- exactly the layout the ring buffer stores, so no colour conversion
# runs on the CPU. drop/max-buffers keep latency bounded if Python stalls.
DEFAULT_GSTREAMER_PIPELINE = (
    "nvarguscamerasrc sensor-id={sensor_id} ! "
    "video/x-raw(memory:NVMM),width={width},height={height},framerate={fps}/1,format=NV12 ! "
    "nvvidconv ! video/x-raw,format=I420 ! "
    "appsink drop=true max-buffers=2 sync=false"
)


class ConfigError(ValueError):
    """The configuration file is invalid."""


def _check(cond: bool, path: str, msg: str) -> None:
    if not cond:
        raise ConfigError(f"{path}: {msg}")


@dataclass
class NodeConfig:
    node_id: str = "node-dev"
    key_hex: Optional[str] = None
    key_file: Optional[str] = None

    def validate(self, path: str) -> None:
        _check(bool(_NODE_ID_RE.match(self.node_id)), f"{path}.node_id", "must be 1-64 chars of [A-Za-z0-9_.:-]")
        _check(not (self.key_hex and self.key_file), path, "set only one of key_hex and key_file")
        if self.key_hex is not None:
            _check(bool(_HEX_KEY_RE.match(self.key_hex)), f"{path}.key_hex", "must be 64 hex characters")

    def load_key(self) -> Optional[bytes]:
        """The 32-byte node key, or None when the node runs unsigned (replay/bench)."""
        text = self.key_hex
        if self.key_file:
            text = Path(self.key_file).read_text(encoding="ascii").strip()
        if text is None:
            return None
        if not _HEX_KEY_RE.match(text):
            raise ConfigError("node key must be 64 hex characters (as returned by POST /api/v1/nodes)")
        return bytes.fromhex(text)


@dataclass
class ServerConfig:
    transport: Literal["udp", "https", "none"] = "udp"
    udp_host: str = "127.0.0.1"
    udp_port: int = 9999
    https_base_url: Optional[str] = None
    report_hz: float = 10.0
    heartbeat_interval_s: float = 10.0
    batch_interval_s: float = 0.1
    command_poll_interval_s: float = 2.0
    request_timeout_s: float = 5.0
    verify_tls: bool = True

    def validate(self, path: str) -> None:
        _check(0 < self.udp_port < 65536, f"{path}.udp_port", "must be a port number")
        _check(0.1 <= self.report_hz <= 120, f"{path}.report_hz", "must be in [0.1, 120]")
        _check(1.0 <= self.heartbeat_interval_s <= 50.0, f"{path}.heartbeat_interval_s",
               "must be in [1, 50] s (the server drops nodes silent for 60 s)")
        _check(0.01 <= self.batch_interval_s <= 5.0, f"{path}.batch_interval_s", "must be in [0.01, 5]")
        if self.transport == "https":
            _check(bool(self.https_base_url), f"{path}.https_base_url", "required for transport https")
            _check(str(self.https_base_url).startswith(("https://", "http://")), f"{path}.https_base_url",
                   "must be an http(s) URL")


@dataclass
class IntrinsicsConfig:
    """Calibrated pinhole + OpenCV distortion model (pixel centres at integers)."""

    fx: float
    fy: float
    cx: float
    cy: float
    dist: List[float] = field(default_factory=list)

    def validate(self, path: str) -> None:
        _check(self.fx > 0 and self.fy > 0, path, "fx and fy must be positive")
        _check(len(self.dist) in (0, 4, 5, 8, 12, 14), f"{path}.dist", "must have 0, 4, 5, 8, 12 or 14 coefficients")


@dataclass
class CameraConfig:
    source: Literal["gstreamer", "v4l2", "file", "synthetic"] = "gstreamer"
    gstreamer_pipeline: Optional[str] = None
    sensor_id: int = 0
    device: str = "/dev/video0"
    file_path: Optional[str] = None
    file_loop: bool = False
    file_start_ns: Optional[int] = None
    realtime: bool = True
    width: int = 1920
    height: int = 1080
    fps: float = 60.0
    capture_latency_ms: float = 0.0
    intrinsics: Optional[IntrinsicsConfig] = None
    fov_h_deg: float = 90.0
    fov_v_deg: Optional[float] = None

    def validate(self, path: str) -> None:
        _check(self.width > 0 and self.height > 0 and self.width % 2 == 0 and self.height % 2 == 0,
               path, "width and height must be positive and even (I420 storage)")
        _check(0 < self.fps <= 1000, f"{path}.fps", "must be in (0, 1000]")
        _check(0 < self.fov_h_deg < 180, f"{path}.fov_h_deg", "must be in (0, 180)")
        _check(self.capture_latency_ms >= 0, f"{path}.capture_latency_ms", "must be >= 0")
        if self.source == "file":
            _check(bool(self.file_path), f"{path}.file_path", "required for source file")

    def pipeline(self) -> str:
        template = self.gstreamer_pipeline or DEFAULT_GSTREAMER_PIPELINE
        return template.format(sensor_id=self.sensor_id, width=self.width, height=self.height,
                               fps=int(round(self.fps)))


@dataclass
class Bno055Config:
    i2c_bus: int = 7
    address: int = 0x28
    poll_hz: float = 20.0
    smoothing_samples: int = 10
    magnetic_declination_deg: float = 0.0
    yaw_offset_deg: float = 0.0
    pitch_offset_deg: float = 0.0
    roll_offset_deg: float = 0.0
    pitch_sign: float = 1.0
    roll_sign: float = 1.0

    def validate(self, path: str) -> None:
        _check(self.address in (0x28, 0x29), f"{path}.address", "BNO055 address is 0x28 or 0x29")
        _check(0 < self.poll_hz <= 100, f"{path}.poll_hz", "must be in (0, 100] (fusion output is 100 Hz)")
        _check(self.pitch_sign in (1.0, -1.0) and self.roll_sign in (1.0, -1.0), path, "signs must be +1 or -1")


@dataclass
class AttitudeConfig:
    source: Literal["static", "bno055"] = "static"
    yaw_deg: float = 0.0
    pitch_deg: float = 45.0
    roll_deg: float = 0.0
    bno055: Bno055Config = field(default_factory=Bno055Config)

    def validate(self, path: str) -> None:
        _check(-90 <= self.pitch_deg <= 90, f"{path}.pitch_deg", "must be in [-90, 90]")
        _check(-180 <= self.roll_deg <= 180, f"{path}.roll_deg", "must be in [-180, 180]")


@dataclass
class PositionConfig:
    latitude: float
    longitude: float
    altitude_m: float

    def validate(self, path: str) -> None:
        _check(-90 <= self.latitude <= 90 and -180 <= self.longitude <= 180, path, "latitude/longitude out of range")
        _check(-1000 <= self.altitude_m <= 12000, f"{path}.altitude_m", "must be metres above the WGS84 ellipsoid")


@dataclass
class GpsConfig:
    source: Literal["gpsd", "nmea", "static"] = "gpsd"
    gpsd_host: str = "127.0.0.1"
    gpsd_port: int = 2947
    nmea_device: str = "/dev/ttyTHS0"
    nmea_baud: int = 9600
    static: Optional[PositionConfig] = None
    max_fix_age_s: float = 10.0

    def validate(self, path: str) -> None:
        if self.source == "static":
            _check(self.static is not None, f"{path}.static", "required for source static")


@dataclass
class SkyMaskConfig:
    min_elevation_deg: Optional[float] = 3.0
    polygon: Optional[List[List[float]]] = None
    polygon_mode: Literal["include", "exclude"] = "include"

    def validate(self, path: str) -> None:
        if self.polygon is not None:
            _check(len(self.polygon) >= 3 and all(len(p) == 2 for p in self.polygon), f"{path}.polygon",
                   "must be a list of at least three [u, v] pixel points")


@dataclass
class DetectionConfig:
    backend: Literal["auto", "cpu", "cuda"] = "auto"
    decimation: int = 2
    background_tau_s: float = 2.0
    background_update_every: int = 2
    foreground_alpha_scale: float = 0.1
    threshold_sigma: float = 5.5
    low_threshold_sigma: float = 3.5
    min_threshold_dn: float = 2.0
    noise_tiles_x: int = 8
    noise_tiles_y: int = 6
    noise_update_every: int = 15
    close_kernel_px: int = 3
    max_blobs: int = 64
    max_foreground_fraction: float = 0.05
    sky_mask: SkyMaskConfig = field(default_factory=SkyMaskConfig)

    def validate(self, path: str) -> None:
        _check(self.decimation in (1, 2, 4), f"{path}.decimation", "must be 1, 2 or 4")
        _check(self.threshold_sigma >= self.low_threshold_sigma > 0, path,
               "need threshold_sigma >= low_threshold_sigma > 0")
        _check(self.background_tau_s > 0 and self.background_update_every >= 1, path, "bad background parameters")
        _check(0 <= self.foreground_alpha_scale <= 1, f"{path}.foreground_alpha_scale", "must be in [0, 1]")
        _check(0 < self.max_foreground_fraction <= 1, f"{path}.max_foreground_fraction", "must be in (0, 1]")


@dataclass
class TrackingConfig:
    gate_sigma: float = 4.0
    max_gate_px: float = 80.0
    measurement_noise_px: float = 0.7
    process_accel_px_s2: float = 600.0
    initial_velocity_px_s: float = 400.0
    confirm_hits: int = 5
    confirm_window: int = 8
    max_coast_s: float = 0.5
    history_s: float = 20.0
    lk_enabled: bool = True
    lk_window_px: int = 15
    lk_levels: int = 3
    dense_flow_every: int = 6
    dense_flow_width: int = 240

    def validate(self, path: str) -> None:
        _check(1 <= self.confirm_hits <= self.confirm_window, path, "need 1 <= confirm_hits <= confirm_window")
        _check(self.lk_window_px % 2 == 1 and self.lk_window_px >= 5, f"{path}.lk_window_px", "must be odd and >= 5")


@dataclass
class SlowFilterConfig:
    enabled: bool = True
    min_angular_speed_deg_s: float = 0.03
    min_age_s: float = 1.5


@dataclass
class CloudFilterConfig:
    enabled: bool = True
    min_effective_area_px: float = 40.0
    max_peak_snr: float = 15.0
    max_angular_speed_deg_s: float = 1.0
    flow_match_px_s: float = 6.0
    flow_match_fraction: float = 0.35


@dataclass
class AircraftFilterConfig:
    enabled: bool = True
    gate_deg: float = 1.0
    baro_gate_deg: float = 2.0
    min_samples: int = 3
    window_s: float = 1.0
    sticky_s: float = 30.0


@dataclass
class BirdFilterConfig:
    enabled: bool = True
    window_s: float = 1.5
    min_area_px: float = 150.0
    expansion_rate_per_s: float = 0.5
    wingbeat_min_hz: float = 2.0
    wingbeat_max_hz: float = 25.0
    wingbeat_min_power: float = 0.35
    wingbeat_min_depth: float = 0.06
    defocus_max_snr_per_px: float = 0.1


@dataclass
class SatelliteFilterConfig:
    enabled: bool = False
    min_age_s: float = 2.0
    min_speed_deg_s: float = 0.1
    max_speed_deg_s: float = 3.0
    max_speed_cv: float = 0.1
    max_cross_track_deg: float = 0.05
    max_intensity_cv: float = 0.35


@dataclass
class MeteorFilterConfig:
    enabled: bool = False
    min_speed_deg_s: float = 8.0
    max_duration_s: float = 3.0
    max_cross_track_deg: float = 0.1


@dataclass
class FiltersConfig:
    slow: SlowFilterConfig = field(default_factory=SlowFilterConfig)
    cloud: CloudFilterConfig = field(default_factory=CloudFilterConfig)
    aircraft: AircraftFilterConfig = field(default_factory=AircraftFilterConfig)
    bird: BirdFilterConfig = field(default_factory=BirdFilterConfig)
    satellite: SatelliteFilterConfig = field(default_factory=SatelliteFilterConfig)
    meteor: MeteorFilterConfig = field(default_factory=MeteorFilterConfig)


@dataclass
class AnomalyConfig:
    window_s: float = 0.5
    min_samples: int = 6
    turn_rate_floor_deg_s: float = 20.0
    turn_rate_scale_deg_s: float = 60.0
    accel_floor_deg_s2: float = 2.0
    accel_scale_deg_s2: float = 10.0
    min_speed_for_turn_deg_s: float = 0.2
    maneuver_turn_rate_deg_s: float = 90.0
    report_unidentified: bool = True
    min_anomaly_score: float = 0.0

    def validate(self, path: str) -> None:
        _check(0 <= self.min_anomaly_score <= 1, f"{path}.min_anomaly_score", "must be in [0, 1]")
        _check(self.min_samples >= 3, f"{path}.min_samples", "must be >= 3 for a quadratic fit")


@dataclass
class RingBufferConfig:
    seconds: float = 10.0
    hold_seconds: float = 4.0
    memory_budget_mb: float = 3500.0
    prefault: bool = True

    def validate(self, path: str) -> None:
        _check(self.seconds > 0 and self.hold_seconds >= 0, path, "seconds must be > 0, hold_seconds >= 0")


@dataclass
class ClipsConfig:
    pre_s: float = 5.0
    post_s: float = 5.0
    max_event_s: float = 60.0
    record_events: bool = True
    storage_dir: str = "/var/lib/skynet/clips"
    retention_max_gb: float = 50.0
    retention_max_age_days: float = 14.0
    ffmpeg: str = "ffmpeg"
    x264_preset: str = "veryfast"
    x264_crf: int = 20
    ffmpeg_threads: int = 2
    low_priority: bool = True
    keep_y4m: bool = False
    upload_max_attempts: int = 6
    frame_wait_timeout_s: float = 20.0

    def validate(self, path: str) -> None:
        _check(self.pre_s >= 0 and self.post_s >= 0, path, "pre_s and post_s must be >= 0")
        _check(self.max_event_s > 0, f"{path}.max_event_s", "must be > 0")
        _check(0 <= self.x264_crf <= 51, f"{path}.x264_crf", "must be in [0, 51]")


@dataclass
class AdsbConfig:
    source: Literal["none", "dump1090", "adsbx_v2"] = "none"
    url: Optional[str] = None
    file_path: Optional[str] = None
    api_key: Optional[str] = None
    api_key_header: str = "api-auth"
    radius_nm: float = 40.0
    poll_interval_s: float = 1.0
    max_position_age_s: float = 15.0
    timeout_s: float = 3.0

    def validate(self, path: str) -> None:
        if self.source == "dump1090":
            _check(bool(self.url) != bool(self.file_path), path, "dump1090 needs exactly one of url or file_path")
        if self.source == "adsbx_v2":
            _check(bool(self.url), f"{path}.url", "required for adsbx_v2 (may contain {lat}, {lon}, {dist})")


@dataclass
class HealthConfig:
    watchdog: bool = True
    stall_timeout_s: float = 10.0
    chrony_offset: bool = True


@dataclass
class EdgeConfig:
    node: NodeConfig = field(default_factory=NodeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    attitude: AttitudeConfig = field(default_factory=AttitudeConfig)
    gps: GpsConfig = field(default_factory=GpsConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    filters: FiltersConfig = field(default_factory=FiltersConfig)
    anomaly: AnomalyConfig = field(default_factory=AnomalyConfig)
    ring_buffer: RingBufferConfig = field(default_factory=RingBufferConfig)
    clips: ClipsConfig = field(default_factory=ClipsConfig)
    adsb: AdsbConfig = field(default_factory=AdsbConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    def validate(self, path: str) -> None:
        span = self.clips.pre_s + 1.0
        _check(self.ring_buffer.seconds >= span, "ring_buffer.seconds",
               f"must be at least clips.pre_s + 1 s ({span:g} s) so the pre-trigger video is still buffered")


# --- generic dataclass builder ------------------------------------------------


def _coerce(tp: Any, value: Any, path: str) -> Any:
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is Union:
        if value is None:
            _check(type(None) in args, path, "may not be null")
            return None
        (inner,) = [a for a in args if a is not type(None)]
        return _coerce(inner, value, path)
    if dataclasses.is_dataclass(tp):
        return _build(tp, value, path)
    if origin is Literal:
        _check(value in args, path, f"must be one of {list(args)}, got {value!r}")
        return value
    if tp is bool:
        _check(isinstance(value, bool), path, f"must be true or false, got {value!r}")
        return value
    if tp is int:
        _check(isinstance(value, int) and not isinstance(value, bool), path, f"must be an integer, got {value!r}")
        return value
    if tp is float:
        _check(isinstance(value, (int, float)) and not isinstance(value, bool), path, f"must be a number, got {value!r}")
        return float(value)
    if tp is str:
        _check(isinstance(value, str), path, f"must be a string, got {value!r}")
        return value
    if origin in (list, List):
        _check(isinstance(value, list), path, "must be a list")
        return [_coerce(args[0], v, f"{path}[{i}]") for i, v in enumerate(value)]
    raise TypeError(f"unsupported config type {tp!r} at {path}")


def _build(cls: Any, data: Any, path: str) -> Any:
    if data is None:
        data = {}
    _check(isinstance(data, dict), path or "<root>", "must be a mapping")
    hints = typing.get_type_hints(cls)
    names = [f.name for f in dataclasses.fields(cls)]
    unknown = sorted(set(data) - set(names))
    _check(not unknown, path or "<root>", f"unknown key(s) {unknown}")
    kwargs = {k: _coerce(hints[k], v, f"{path}.{k}" if path else k) for k, v in data.items()}
    try:
        obj = cls(**kwargs)
    except TypeError as exc:
        missing = [f.name for f in dataclasses.fields(cls)
                   if f.name not in kwargs and f.default is dataclasses.MISSING
                   and f.default_factory is dataclasses.MISSING]  # type: ignore[misc]
        raise ConfigError(f"{path or '<root>'}: missing required key(s) {missing}") from exc
    validate = getattr(obj, "validate", None)
    if validate is not None:
        validate(path or "<root>")
    return obj


def parse_config(data: Optional[Dict[str, Any]]) -> EdgeConfig:
    """Build and validate an EdgeConfig from an already-parsed mapping."""
    return _build(EdgeConfig, data, "")


def load_config(path: Union[str, Path]) -> EdgeConfig:
    """Read and validate a YAML configuration file."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    return parse_config(data)


def to_dict(cfg: Any) -> Dict[str, Any]:
    """Plain-dict view of a config (for logging and the bench report)."""
    return dataclasses.asdict(cfg)


def frames_for(seconds: float, fps: float) -> int:
    return max(1, int(round(seconds * fps)))


def ring_memory_bytes(width: int, height: int, slots: int) -> int:
    """Bytes needed for ``slots`` I420 frames."""
    return width * height * 3 // 2 * slots


def ring_slots(cfg: EdgeConfig) -> Tuple[int, int]:
    """(window frames, hold frames) of the ring buffer for this config."""
    fps = cfg.camera.fps
    return frames_for(cfg.ring_buffer.seconds, fps), int(round(cfg.ring_buffer.hold_seconds * fps))
