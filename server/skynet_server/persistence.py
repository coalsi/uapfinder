"""PostGIS persistence via asyncpg (optional).

The live system runs entirely on the state backend; the database is the
durable record of nodes, triangulations, tracks and clips, and serves
history older than the state backend's retention. The server works without
it (``/health`` reports ``"db": "disabled"``).

Write paths:

* standalone: ``BatchWriter`` collects rows from the ingest pipeline and
  flushes them every ``SKYNET_DB_BATCH_MS`` in one transaction, coalescing
  repeated track/node updates so a busy track costs one upsert per flush;
* vercel: ``Database.write`` is awaited before the HTTP response is sent,
  because a Function may be frozen as soon as it has responded.

Every multi-row write is a single ``INSERT ... SELECT FROM unnest(...)``
statement. That is one round trip regardless of row count and works through
transaction-mode poolers (Neon's PgBouncer) once the asyncpg statement cache
is disabled, which happens automatically for pooled endpoints.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import orjson

from .models import ClipRecord, Detection, NodeRecord, TrackState

log = logging.getLogger("skynet.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# libpq connection parameters asyncpg does not understand; asyncpg would
# forward them to the server as runtime settings, which fails the startup.
_LIBPQ_ONLY_PARAMS = {"channel_binding", "connect_timeout", "gssencmode", "sslcompression", "requiressl",
                      "keepalives", "keepalives_idle", "keepalives_interval", "keepalives_count", "pgbouncer"}


def clean_dsn(dsn: str) -> str:
    parts = urlsplit(dsn)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in _LIBPQ_ONLY_PARAMS]
    return urlunsplit(parts._replace(query=urlencode(query)))


def behind_pooler(dsn: str) -> bool:
    parts = urlsplit(dsn)
    host = parts.hostname or ""
    return "-pooler" in host or "pooler." in host or parts.port == 6432


@dataclass
class DbBatch:
    nodes: Dict[str, NodeRecord] = field(default_factory=dict)
    triangulations: List[Dict[str, Any]] = field(default_factory=list)
    tracks: Dict[str, TrackState] = field(default_factory=dict)
    clips: Dict[str, ClipRecord] = field(default_factory=dict)
    detections: List[Detection] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.nodes or self.triangulations or self.tracks or self.clips or self.detections)

    def merge(self, other: "DbBatch") -> None:
        self.nodes.update(other.nodes)
        self.triangulations.extend(other.triangulations)
        self.tracks.update(other.tracks)
        self.clips.update(other.clips)
        self.detections.extend(other.detections)

    def row_count(self) -> int:
        return len(self.nodes) + len(self.triangulations) + len(self.tracks) + len(self.clips) + len(self.detections)


_UPSERT_NODES = """
INSERT INTO nodes (node_id, label, registered_ns, last_seen_ns, position, lat, lon, alt_m,
                   yaw_deg, pitch_deg, roll_deg, fov_h_deg, fov_v_deg, software_version, status, updated_at)
SELECT u.node_id, u.label, u.registered_ns, u.last_seen_ns,
       CASE WHEN u.lat IS NULL THEN NULL
            ELSE ST_SetSRID(ST_MakePoint(u.lon, u.lat, u.alt_m), 4326)::geography END,
       u.lat, u.lon, u.alt_m, u.yaw_deg, u.pitch_deg, u.roll_deg, u.fov_h_deg, u.fov_v_deg,
       u.software_version, COALESCE(u.status, '{}')::jsonb, now()
FROM unnest($1::text[], $2::text[], $3::bigint[], $4::bigint[], $5::float8[], $6::float8[], $7::float8[],
            $8::float8[], $9::float8[], $10::float8[], $11::float8[], $12::float8[], $13::text[], $14::text[])
     AS u(node_id, label, registered_ns, last_seen_ns, lat, lon, alt_m, yaw_deg, pitch_deg, roll_deg,
          fov_h_deg, fov_v_deg, software_version, status)
ON CONFLICT (node_id) DO UPDATE SET
    label = COALESCE(EXCLUDED.label, nodes.label),
    registered_ns = COALESCE(nodes.registered_ns, EXCLUDED.registered_ns),
    last_seen_ns = GREATEST(nodes.last_seen_ns, EXCLUDED.last_seen_ns),
    position = COALESCE(EXCLUDED.position, nodes.position),
    lat = COALESCE(EXCLUDED.lat, nodes.lat),
    lon = COALESCE(EXCLUDED.lon, nodes.lon),
    alt_m = COALESCE(EXCLUDED.alt_m, nodes.alt_m),
    yaw_deg = COALESCE(EXCLUDED.yaw_deg, nodes.yaw_deg),
    pitch_deg = COALESCE(EXCLUDED.pitch_deg, nodes.pitch_deg),
    roll_deg = COALESCE(EXCLUDED.roll_deg, nodes.roll_deg),
    fov_h_deg = COALESCE(EXCLUDED.fov_h_deg, nodes.fov_h_deg),
    fov_v_deg = COALESCE(EXCLUDED.fov_v_deg, nodes.fov_v_deg),
    software_version = COALESCE(EXCLUDED.software_version, nodes.software_version),
    status = EXCLUDED.status,
    updated_at = now()
"""

_INSERT_TRIANGULATIONS = """
INSERT INTO triangulations (event_id, track_id, t_ns, position, lat, lon, alt_m, sigma_m, error_margin_m,
                            parallax_deg, node_ids, rays)
SELECT u.event_id, u.track_id, u.t_ns, ST_SetSRID(ST_MakePoint(u.lon, u.lat, u.alt_m), 4326)::geography,
       u.lat, u.lon, u.alt_m, u.sigma_m, u.error_margin_m, u.parallax_deg,
       string_to_array(u.node_ids, ','), u.rays::jsonb
FROM unnest($1::text[], $2::text[], $3::bigint[], $4::float8[], $5::float8[], $6::float8[], $7::float8[],
            $8::float8[], $9::float8[], $10::text[], $11::text[])
     AS u(event_id, track_id, t_ns, lat, lon, alt_m, sigma_m, error_margin_m, parallax_deg, node_ids, rays)
ON CONFLICT (event_id) DO NOTHING
"""

_UPSERT_TRACKS = """
INSERT INTO tracks (track_id, first_ns, last_ns, node_ids, point_count, speed_mps, max_speed_mps,
                    max_accel_mps2, classification, flags, last_point, updated_at)
SELECT u.track_id, u.first_ns, u.last_ns, string_to_array(u.node_ids, ','), u.point_count, u.speed_mps,
       u.max_speed_mps, u.max_accel_mps2, u.classification, string_to_array(u.flags, ','),
       u.last_point::jsonb, now()
FROM unnest($1::text[], $2::bigint[], $3::bigint[], $4::text[], $5::int[], $6::float8[], $7::float8[],
            $8::float8[], $9::text[], $10::text[], $11::text[])
     AS u(track_id, first_ns, last_ns, node_ids, point_count, speed_mps, max_speed_mps, max_accel_mps2,
          classification, flags, last_point)
ON CONFLICT (track_id) DO UPDATE SET
    first_ns = LEAST(tracks.first_ns, EXCLUDED.first_ns),
    last_ns = GREATEST(tracks.last_ns, EXCLUDED.last_ns),
    node_ids = EXCLUDED.node_ids,
    point_count = GREATEST(tracks.point_count, EXCLUDED.point_count),
    speed_mps = EXCLUDED.speed_mps,
    max_speed_mps = GREATEST(tracks.max_speed_mps, EXCLUDED.max_speed_mps),
    max_accel_mps2 = GREATEST(tracks.max_accel_mps2, EXCLUDED.max_accel_mps2),
    classification = EXCLUDED.classification,
    flags = EXCLUDED.flags,
    last_point = EXCLUDED.last_point,
    updated_at = now()
"""

_UPSERT_CLIPS = """
INSERT INTO clips (request_id, event_id, track_id, node_id, status, t_start_ns, t_end_ns, issued_ns, bytes,
                   sha256, content_type, frames, storage_key, uploaded_ns, updated_at)
SELECT u.*, now()
FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[], $6::bigint[], $7::bigint[],
            $8::bigint[], $9::bigint[], $10::text[], $11::text[], $12::int[], $13::text[], $14::bigint[])
     AS u(request_id, event_id, track_id, node_id, status, t_start_ns, t_end_ns, issued_ns, bytes, sha256,
          content_type, frames, storage_key, uploaded_ns)
ON CONFLICT (request_id) DO UPDATE SET
    status = EXCLUDED.status, bytes = EXCLUDED.bytes, sha256 = EXCLUDED.sha256,
    content_type = EXCLUDED.content_type, frames = EXCLUDED.frames, storage_key = EXCLUDED.storage_key,
    uploaded_ns = EXCLUDED.uploaded_ns, updated_at = now()
"""

_INSERT_DETECTIONS = """
INSERT INTO detections (node_id, t_ns, seq, track_id, lat, lon, alt_m, az_deg, el_deg)
SELECT * FROM unnest($1::text[], $2::bigint[], $3::bigint[], $4::text[], $5::float8[], $6::float8[],
                     $7::float8[], $8::float8[], $9::float8[])
ON CONFLICT DO NOTHING
"""

_VOLUNTEER_FIELDS = ("email", "name", "country", "region", "city", "postal_code", "approx_lat", "approx_lon",
                     "site_type", "has_hardware", "interests", "notes", "consent")

_UPSERT_VOLUNTEER = f"""
INSERT INTO volunteers ({", ".join(_VOLUNTEER_FIELDS)})
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::text[], $12, $13)
ON CONFLICT (email) DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in _VOLUNTEER_FIELDS[1:])},
    updated_at = now()
"""

_TRACK_COLUMNS = ("track_id, first_ns, last_ns, node_ids, point_count, speed_mps, max_speed_mps, max_accel_mps2, "
                  "classification, flags, last_point")


def _dumps(obj: Any) -> str:
    return orjson.dumps(obj).decode()


def _track_row(r: Any) -> Dict[str, Any]:
    return {
        "track_id": r["track_id"],
        "first_ns": r["first_ns"],
        "last_ns": r["last_ns"],
        "node_ids": list(r["node_ids"]),
        "point_count": r["point_count"],
        "last_point": orjson.loads(r["last_point"]) if r["last_point"] else None,
        "speed_mps": r["speed_mps"],
        "max_speed_mps": r["max_speed_mps"],
        "max_accel_mps2": r["max_accel_mps2"],
        "classification": r["classification"],
        "flags": list(r["flags"]),
    }


class Database:
    def __init__(self, url: str, statement_cache: Optional[int] = None, store_detections: bool = False,
                 max_size: int = 5):
        self.url = clean_dsn(url)
        self.statement_cache = statement_cache if statement_cache is not None else (0 if behind_pooler(url) else 100)
        self.store_detections = store_detections
        self.max_size = max_size
        self.pool: Any = None

    async def start(self) -> None:
        import asyncpg

        self.pool = await asyncpg.create_pool(
            self.url, min_size=0, max_size=self.max_size, statement_cache_size=self.statement_cache,
            command_timeout=30,
        )

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def ping(self) -> bool:
        try:
            async with self.pool.acquire() as conn:
                return await asyncio.wait_for(conn.fetchval("SELECT 1"), 3.0) == 1
        except Exception:  # noqa: BLE001 - health reporting must not raise
            log.exception("database ping failed")
            return False

    async def write(self, batch: DbBatch) -> None:
        if batch.is_empty():
            return
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if batch.nodes:
                    nodes = list(batch.nodes.values())
                    await conn.execute(
                        _UPSERT_NODES,
                        [n.node_id for n in nodes], [n.label for n in nodes], [n.registered_ns for n in nodes],
                        [n.last_seen_ns or None for n in nodes], [n.lat for n in nodes], [n.lon for n in nodes],
                        [n.alt_m for n in nodes], [n.yaw_deg for n in nodes], [n.pitch_deg for n in nodes],
                        [n.roll_deg for n in nodes], [n.fov_h_deg for n in nodes], [n.fov_v_deg for n in nodes],
                        [n.software_version for n in nodes], [_dumps(n.status or {}) for n in nodes],
                    )
                if batch.triangulations:
                    tri = batch.triangulations
                    await conn.execute(
                        _INSERT_TRIANGULATIONS,
                        [t["event_id"] for t in tri], [t["track_id"] for t in tri], [t["t_ns"] for t in tri],
                        [t["lat"] for t in tri], [t["lon"] for t in tri], [t["alt_m"] for t in tri],
                        [t["sigma_m"] for t in tri], [t["error_margin_m"] for t in tri],
                        [t["parallax_deg"] for t in tri], [",".join(t["node_ids"]) for t in tri],
                        [_dumps(t["rays"]) for t in tri],
                    )
                if batch.tracks:
                    tracks = list(batch.tracks.values())
                    await conn.execute(
                        _UPSERT_TRACKS,
                        [t.track_id for t in tracks], [t.first_ns for t in tracks], [t.last_ns for t in tracks],
                        [",".join(t.node_ids) for t in tracks], [t.point_count for t in tracks],
                        [t.speed_mps for t in tracks], [t.max_speed_mps for t in tracks],
                        [t.max_accel_mps2 for t in tracks], [t.classification for t in tracks],
                        [",".join(t.flags) for t in tracks], [_dumps(t.last_point) for t in tracks],
                    )
                if batch.clips:
                    clips = list(batch.clips.values())
                    await conn.execute(
                        _UPSERT_CLIPS,
                        [c.request_id for c in clips], [c.event_id for c in clips], [c.track_id for c in clips],
                        [c.node_id for c in clips], [c.status for c in clips], [c.t_start_ns for c in clips],
                        [c.t_end_ns for c in clips], [c.issued_ns for c in clips], [c.bytes for c in clips],
                        [c.sha256 for c in clips], [c.content_type for c in clips], [c.frames for c in clips],
                        [c.storage_key for c in clips], [c.uploaded_ns for c in clips],
                    )
                if batch.detections and self.store_detections:
                    d = batch.detections
                    await conn.execute(
                        _INSERT_DETECTIONS,
                        [x.node_id for x in d], [x.t_ns for x in d], [x.seq for x in d], [x.track_id for x in d],
                        [x.lat for x in d], [x.lon for x in d], [x.alt_m for x in d], [x.az_deg for x in d],
                        [x.el_deg for x in d],
                    )

    # --- reads (history beyond the state backend's retention) -----------------

    async def get_track(self, track_id: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(f"SELECT {_TRACK_COLUMNS} FROM tracks WHERE track_id = $1", track_id)
        return _track_row(row) if row else None

    async def list_tracks(self, since_ns: int, limit: int, classification: Optional[str]) -> List[Dict[str, Any]]:
        sql = f"SELECT {_TRACK_COLUMNS} FROM tracks WHERE last_ns >= $1"
        args: List[Any] = [since_ns]
        if classification:
            sql += " AND classification = $2"
            args.append(classification)
        sql += f" ORDER BY last_ns DESC LIMIT {int(limit)}"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [_track_row(r) for r in rows]

    async def track_points(self, track_id: str, limit: int) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT t_ns, lat, lon, alt_m, sigma_m, error_margin_m, node_ids FROM ("
                " SELECT * FROM triangulations WHERE track_id = $1 ORDER BY t_ns DESC LIMIT $2"
                ") recent ORDER BY t_ns",
                track_id, limit,
            )
        return [
            {"t_ns": r["t_ns"], "lat": r["lat"], "lon": r["lon"], "alt_m": r["alt_m"], "sigma_m": r["sigma_m"],
             "error_margin_m": r["error_margin_m"], "node_ids": list(r["node_ids"])}
            for r in rows
        ]

    async def track_clips(self, track_id: str) -> List[ClipRecord]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT request_id, event_id, track_id, node_id, t_start_ns, t_end_ns, issued_ns, status, bytes,"
                " sha256, content_type, frames, storage_key, uploaded_ns FROM clips WHERE track_id = $1"
                " ORDER BY issued_ns", track_id)
        return [ClipRecord(**dict(r)) for r in rows]

    async def get_clip(self, request_id: str) -> Optional[ClipRecord]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT request_id, event_id, track_id, node_id, t_start_ns, t_end_ns, issued_ns, status, bytes,"
                " sha256, content_type, frames, storage_key, uploaded_ns FROM clips WHERE request_id = $1",
                request_id)
        return ClipRecord(**dict(row)) if row else None

    async def list_nodes(self) -> List[NodeRecord]:
        """Registered and previously seen nodes (survives restarts of the memory backend)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT node_id, label, lat, lon, alt_m, yaw_deg, pitch_deg, roll_deg, fov_h_deg, fov_v_deg,"
                " last_seen_ns, status, software_version, registered_ns FROM nodes")
        return [NodeRecord(r["node_id"], r["label"], r["lat"], r["lon"], r["alt_m"], r["yaw_deg"], r["pitch_deg"],
                           r["roll_deg"], r["fov_h_deg"], r["fov_v_deg"], r["last_seen_ns"] or 0,
                           orjson.loads(r["status"]) if r["status"] else {}, r["software_version"],
                           r["registered_ns"]) for r in rows]

    # --- volunteers ---------------------------------------------------------

    async def upsert_volunteer(self, v: Dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(_UPSERT_VOLUNTEER, *[v[name] for name in _VOLUNTEER_FIELDS])

    async def volunteer_summary(self, min_count: int) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT count(*) FROM volunteers")
            rows = await conn.fetch(
                "SELECT least(floor(approx_lat), 89) AS la, least(floor(approx_lon), 179) AS lo, count(*) AS n"
                " FROM volunteers WHERE approx_lat IS NOT NULL AND approx_lon IS NOT NULL"
                " GROUP BY 1, 2 HAVING count(*) >= $1 ORDER BY 1, 2", min_count)
        return {"total": int(total),
                "cells": [{"lat": r["la"] + 0.5, "lon": r["lo"] + 0.5, "count": int(r["n"])} for r in rows]}

    async def list_volunteers(self) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT id, {', '.join(_VOLUNTEER_FIELDS)}, created_at, updated_at"
                                    " FROM volunteers ORDER BY id")
        return [{**dict(r), "interests": list(r["interests"])} for r in rows]

    async def delete_volunteer(self, volunteer_id: int) -> bool:
        async with self.pool.acquire() as conn:
            return await conn.execute("DELETE FROM volunteers WHERE id = $1", volunteer_id) == "DELETE 1"


async def migrate(url: str, directory: Path = MIGRATIONS_DIR) -> List[str]:
    """Apply pending ``NNN_*.sql`` files in order; returns the applied versions.

    Each file runs in its own transaction with the simple query protocol, so
    it can contain several statements and works through a pooler; Neon's
    docs still recommend the direct (unpooled) URL for schema changes.
    """
    import asyncpg

    conn = await asyncpg.connect(clean_dsn(url), statement_cache_size=0)
    applied: List[str] = []
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
        for path in sorted(directory.glob("*.sql")):
            version = path.stem
            if version in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
            applied.append(version)
    finally:
        await conn.close()
    return applied


class BatchWriter:
    """Coalescing background writer used in standalone mode."""

    def __init__(self, db: Database, interval_ms: int = 500, max_pending_rows: int = 200_000):
        self.db = db
        self.interval_s = interval_ms / 1000.0
        self.max_pending_rows = max_pending_rows
        self._pending = DbBatch()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self.dropped_rows = 0
        self.failures = 0

    def submit(self, batch: DbBatch) -> None:
        if self._pending.row_count() > self.max_pending_rows:
            # The database cannot keep up; shed load instead of growing without bound.
            self.dropped_rows += batch.row_count()
            return
        self._pending.merge(batch)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(), name="skynet-db-writer")

    async def flush(self) -> None:
        batch, self._pending = self._pending, DbBatch()
        if batch.is_empty():
            return
        try:
            await self.db.write(batch)
        except Exception:  # noqa: BLE001 - keep ingesting when the database is down
            self.failures += 1
            self.dropped_rows += batch.row_count()
            log.exception("database write failed; %d rows dropped", batch.row_count())

    async def _run(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.interval_s)
            await self.flush()

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.flush()
