"""Server configuration, read once from ``SKYNET_*`` environment variables.

Everything is configured through the environment so that the same code runs
unchanged as a standalone process (launchd, systemd, a container) and as a
Vercel Function, where the environment is the only configuration channel.
Marketplace integrations on Vercel inject their own variable names
(``REDIS_URL`` / ``KV_URL`` for Upstash, ``DATABASE_URL`` / ``POSTGRES_URL``
for Neon), so those are accepted as fallbacks behind the explicit
``SKYNET_*`` names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional, Tuple

from skynet_common.geodesy import METERS_PER_MILE
from skynet_common.triangulation import GateConfig

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """An environment variable has an invalid value."""


def _first(env: Mapping[str, str], *names: str) -> Optional[str]:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return None


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


@dataclass(frozen=True)
class MatchConfig:
    """Matchmaker parameters (blueprint section 3)."""

    radius_m: float = 80.0 * METERS_PER_MILE
    window_ns: int = 50_000_000
    interp_max_gap_ns: int = 250_000_000
    retention_ns: int = 5_000_000_000
    pair_ttl_ms: int = 15_000
    # Rays per solution (the packet's own plus the best neighbours). Accuracy
    # improves only as 1/sqrt(n) while cost grows with n per packet and n^2
    # per frame, so an object seen by dozens of nodes is solved from the best
    # few; every node still contributes through its own packets.
    max_rays: int = 8
    gate: GateConfig = field(default_factory=GateConfig)


@dataclass(frozen=True)
class TrackConfig:
    link_ttl_ms: int = 30_000
    fit_window_ns: int = 3_000_000_000  # full-rate samples kept for the acceleration fit
    max_recent_points: int = 96
    max_points: int = 2_000
    event_interval_ns: int = 1_000_000_000
    retention_ns: int = 6 * 3600 * 1_000_000_000


@dataclass(frozen=True)
class S3Config:
    endpoint: str
    bucket: str
    region: str
    access_key_id: str
    secret_access_key: str
    path_style: bool
    prefix: str = "clips/"
    public_base_url: Optional[str] = None
    verify_uploads: bool = True


@dataclass(frozen=True)
class Settings:
    mode: str = "standalone"  # "standalone" | "vercel"
    state_backend: str = "memory"  # "memory" | "redis"
    redis_url: Optional[str] = None
    redis_prefix: str = "skynet:"
    database_url: Optional[str] = None
    database_url_unpooled: Optional[str] = None  # direct connection for migrations (Neon)
    db_statement_cache: Optional[int] = None  # None = automatic (0 behind a pooler)
    store_detections: bool = False
    db_batch_ms: int = 500
    master_key: Optional[bytes] = None
    admin_token: Optional[str] = None
    allow_unsigned: bool = False
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    udp_host: str = "0.0.0.0"
    udp_port: Optional[int] = 9999
    udp_rcvbuf: int = 8 * 1024 * 1024
    udp_batch: int = 64
    udp_public_endpoint: Optional[str] = None
    public_url: Optional[str] = None
    public_location_decimals: int = 2
    cors_origins: Tuple[str, ...] = ()
    trust_proxy: bool = False
    freshness_ns: int = 10_000_000_000
    node_active_ns: int = 60_000_000_000
    http_auth_window_ns: int = 60_000_000_000
    clip_window_ns: int = 5_000_000_000
    events_maxlen: int = 10_000
    live_poll_ms: int = 500
    ws_max_s: Optional[float] = None
    volunteer_rate_per_hour: int = 5
    storage: str = "local"  # "local" | "s3" | "none"
    storage_dir: str = "data/clips"
    max_clip_bytes: int = 512 * 1024 * 1024
    s3: Optional[S3Config] = None
    log_level: str = "info"
    match: MatchConfig = field(default_factory=MatchConfig)
    tracks: TrackConfig = field(default_factory=TrackConfig)

    @property
    def udp_enabled(self) -> bool:
        return self.mode == "standalone" and bool(self.udp_port)

    @property
    def replay_window_ns(self) -> int:
        """How long (node, seq, timestamp) triples are remembered.

        A message older than the freshness window is rejected as stale before
        the replay check, so remembering keys for twice that window (a message
        can be up to ``freshness`` in the future or the past) plus a margin
        catches every replay.
        """
        return 2 * self.freshness_ns + 10_000_000_000

    def with_overrides(self, **kwargs: Any) -> "Settings":
        return replace(self, **kwargs)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        on_vercel = bool(env.get("VERCEL"))
        mode = (env.get("SKYNET_MODE") or ("vercel" if on_vercel else "standalone")).strip().lower()
        if mode not in ("standalone", "vercel"):
            raise ConfigError("SKYNET_MODE must be 'standalone' or 'vercel'")

        redis_url = _first(env, "SKYNET_REDIS_URL", "REDIS_URL", "KV_URL")
        backend = (env.get("SKYNET_STATE_BACKEND") or ("redis" if mode == "vercel" else "memory")).strip().lower()
        if backend not in ("memory", "redis"):
            raise ConfigError("SKYNET_STATE_BACKEND must be 'memory' or 'redis'")
        if backend == "redis" and not redis_url:
            raise ConfigError("the redis state backend needs SKYNET_REDIS_URL (or REDIS_URL / KV_URL)")
        if mode == "vercel" and backend == "memory":
            raise ConfigError("vercel mode keeps no state between invocations; use the redis backend")

        master = env.get("SKYNET_MASTER_KEY", "").strip()
        if master and len(master) < 32:
            raise ConfigError("SKYNET_MASTER_KEY must be at least 32 characters (use `skynet-server keygen`)")

        udp_port: Optional[int] = None
        if mode == "standalone":
            udp_port = _int(env, "SKYNET_UDP_PORT", 9999) or None

        public_url = _first(env, "SKYNET_PUBLIC_URL")
        if public_url is None and on_vercel:
            host = _first(env, "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL")
            public_url = f"https://{host}" if host else None

        cors = tuple(o.strip().rstrip("/") for o in env.get("SKYNET_CORS_ORIGINS", "").split(",") if o.strip())

        default_storage = "s3" if env.get("SKYNET_S3_BUCKET") else ("none" if mode == "vercel" else "local")
        storage = (env.get("SKYNET_STORAGE") or default_storage).strip().lower()
        if storage not in ("local", "s3", "none"):
            raise ConfigError("SKYNET_STORAGE must be 'local', 's3' or 'none'")
        if storage == "local" and mode == "vercel":
            raise ConfigError("vercel functions have no persistent disk; use SKYNET_STORAGE=s3")
        s3 = None
        if storage == "s3":
            bucket = _first(env, "SKYNET_S3_BUCKET")
            key_id = _first(env, "SKYNET_S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
            secret = _first(env, "SKYNET_S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
            if not (bucket and key_id and secret):
                raise ConfigError(
                    "s3 storage needs SKYNET_S3_BUCKET, SKYNET_S3_ACCESS_KEY_ID and SKYNET_S3_SECRET_ACCESS_KEY")
            region = _first(env, "SKYNET_S3_REGION", "AWS_REGION") or "us-east-1"
            endpoint = _first(env, "SKYNET_S3_ENDPOINT")
            public_base = _first(env, "SKYNET_S3_PUBLIC_URL")
            s3 = S3Config(
                endpoint=(endpoint or f"https://s3.{region}.amazonaws.com").rstrip("/"),
                bucket=bucket,
                region=region,
                access_key_id=key_id,
                secret_access_key=secret,
                # Custom endpoints (R2, B2, MinIO) are addressed path-style;
                # AWS itself prefers virtual-hosted buckets.
                path_style=_bool(env, "SKYNET_S3_PATH_STYLE", default=endpoint is not None),
                prefix=env.get("SKYNET_S3_PREFIX", "clips/"),
                public_base_url=public_base.rstrip("/") if public_base else None,
                verify_uploads=_bool(env, "SKYNET_S3_VERIFY_UPLOADS", True),
            )

        gate = GateConfig(
            angular_sigma_deg=_float(env, "SKYNET_ANGULAR_SIGMA_DEG", 0.05),
            k_sigma=_float(env, "SKYNET_GATE_K_SIGMA", 3.0),
            miss_floor_m=_float(env, "SKYNET_GATE_MISS_FLOOR_M", 30.0),
            min_parallax_deg=_float(env, "SKYNET_GATE_MIN_PARALLAX_DEG", 1.0),
        )
        match = MatchConfig(
            radius_m=_float(env, "SKYNET_MATCH_RADIUS_MILES", 80.0) * METERS_PER_MILE,
            window_ns=int(_float(env, "SKYNET_MATCH_WINDOW_MS", 50.0) * 1e6),
            interp_max_gap_ns=int(_float(env, "SKYNET_INTERP_MAX_GAP_MS", 250.0) * 1e6),
            retention_ns=int(_float(env, "SKYNET_DETECTION_RETENTION_S", 5.0) * 1e9),
            max_rays=max(2, _int(env, "SKYNET_MATCH_MAX_RAYS", 8)),
            gate=gate,
        )
        tracks = TrackConfig(
            link_ttl_ms=int(_float(env, "SKYNET_TRACK_LINK_TTL_S", 30.0) * 1000),
            event_interval_ns=int(_float(env, "SKYNET_TRACK_EVENT_INTERVAL_MS", 1000.0) * 1e6),
            retention_ns=int(_float(env, "SKYNET_TRACK_RETENTION_H", 6.0) * 3600e9),
        )
        cache = env.get("SKYNET_DB_STATEMENT_CACHE", "").strip()
        ws_max = _float(env, "SKYNET_WS_MAX_S", 280.0 if mode == "vercel" else 0.0)
        return cls(
            mode=mode,
            state_backend=backend,
            redis_url=redis_url,
            redis_prefix=env.get("SKYNET_REDIS_PREFIX", "skynet:"),
            database_url=_first(env, "SKYNET_DATABASE_URL", "DATABASE_URL", "POSTGRES_URL"),
            database_url_unpooled=_first(env, "SKYNET_DATABASE_URL_UNPOOLED", "DATABASE_URL_UNPOOLED",
                                         "POSTGRES_URL_NON_POOLING"),
            db_statement_cache=int(cache) if cache else None,
            store_detections=_bool(env, "SKYNET_STORE_DETECTIONS", False),
            db_batch_ms=_int(env, "SKYNET_DB_BATCH_MS", 500),
            master_key=master.encode("utf-8") if master else None,
            admin_token=env.get("SKYNET_ADMIN_TOKEN", "").strip() or None,
            allow_unsigned=_bool(env, "SKYNET_ALLOW_UNSIGNED", False),
            http_host=env.get("SKYNET_HTTP_HOST", "0.0.0.0"),
            http_port=_int(env, "SKYNET_HTTP_PORT", 8000),
            udp_host=env.get("SKYNET_UDP_HOST", "0.0.0.0"),
            udp_port=udp_port,
            udp_rcvbuf=_int(env, "SKYNET_UDP_RCVBUF", 8 * 1024 * 1024),
            udp_batch=max(1, _int(env, "SKYNET_UDP_BATCH", 64)),
            udp_public_endpoint=_first(env, "SKYNET_UDP_PUBLIC_ENDPOINT"),
            public_url=public_url.rstrip("/") if public_url else None,
            public_location_decimals=_int(env, "SKYNET_PUBLIC_LOCATION_DECIMALS", 2),
            cors_origins=cors,
            # Behind Vercel's proxy the socket peer is always the local runtime.
            trust_proxy=_bool(env, "SKYNET_TRUST_PROXY", mode == "vercel"),
            freshness_ns=int(_float(env, "SKYNET_FRESHNESS_S", 10.0) * 1e9),
            node_active_ns=int(_float(env, "SKYNET_NODE_ACTIVE_S", 60.0) * 1e9),
            clip_window_ns=int(_float(env, "SKYNET_CLIP_WINDOW_S", 5.0) * 1e9),
            events_maxlen=_int(env, "SKYNET_EVENTS_MAXLEN", 10_000),
            live_poll_ms=_int(env, "SKYNET_LIVE_POLL_MS", 1000 if mode == "vercel" else 500),
            ws_max_s=ws_max if ws_max > 0 else None,
            volunteer_rate_per_hour=_int(env, "SKYNET_VOLUNTEER_RATE_PER_HOUR", 5),
            storage=storage,
            storage_dir=env.get("SKYNET_STORAGE_DIR", "data/clips"),
            max_clip_bytes=_int(env, "SKYNET_MAX_CLIP_BYTES", 512 * 1024 * 1024),
            s3=s3,
            log_level=env.get("SKYNET_LOG_LEVEL", "info").strip().lower(),
            match=match,
            tracks=tracks,
        )
