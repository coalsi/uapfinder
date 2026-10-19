"""PostGIS persistence and migrations against a throwaway local cluster."""

from __future__ import annotations

import time

import pytest

from skynet_server.models import ClipRecord, Detection, NodeRecord, TrackState, track_point
from skynet_server.persistence import BatchWriter, Database, DbBatch, migrate

asyncpg = pytest.importorskip("asyncpg")
NOW = time.time_ns()


async def test_migrations_are_idempotent(database_url):
    assert await migrate(database_url) == ["001_init", "002_volunteers"]
    assert await migrate(database_url) == []
    conn = await asyncpg.connect(database_url)
    try:
        tables = {r["tablename"] for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        assert {"nodes", "tracks", "triangulations", "clips", "detections", "volunteers",
                "schema_migrations"} <= tables
        coltype = await conn.fetchval(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'triangulations'::regclass AND attname = 'position'")
        assert coltype == "geography(PointZ,4326)"
        index = await conn.fetchval("SELECT indexdef FROM pg_indexes WHERE indexname = 'triangulations_position_gix'")
        assert "USING gist" in index
    finally:
        await conn.close()


async def test_batch_write_and_history_reads(database_url):
    await migrate(database_url)
    db = Database(database_url, store_detections=True)
    await db.start()
    try:
        assert await db.ping()
        node = NodeRecord("node-a", "Roof", 33.6292, -112.3679, 357.2, 10.0, 45.0, 0.0, 90.0, 58.7, NOW,
                          {"fps": 30}, "0.1.0", NOW - 1000)
        track = TrackState("T-1", NOW, NOW, NOW + 100, ["node-a", "node-b"], 2,
                           track_point(NOW + 100, 33.55, -112.2, 9144.0, 41.2, 12.5, ["node-a", "node-b"]),
                           speed_mps=2210.0, max_speed_mps=2380.5, max_accel_mps2=12.0, classification="hypersonic",
                           flags=["hypersonic"])
        tri = [{"event_id": f"e{i}", "track_id": "T-1", "t_ns": NOW + i * 100, "lat": 33.55, "lon": -112.2 + i * 1e-3,
                "alt_m": 9144.0, "sigma_m": 41.2, "error_margin_m": 12.5, "parallax_deg": 30.0,
                "node_ids": ["node-a", "node-b"], "rays": [{"node_id": "node-a", "az_deg": 1.0}]} for i in range(2)]
        clip = ClipRecord("r1", "e0", "T-1", "node-a", NOW - 5, NOW + 5, NOW)
        det = Detection("node-a", NOW, 7, "t1", 33.6, -112.3, 357.0, 120.0, 45.0, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0))
        await db.write(DbBatch(nodes={"node-a": node}, triangulations=tri, tracks={"T-1": track},
                               clips={"r1": clip}, detections=[det]))
        # Upserts coalesce: a later write updates the same rows.
        track.point_count, track.last_ns = 3, NOW + 200
        clip.status, clip.bytes = "uploaded", 1234
        await db.write(DbBatch(tracks={"T-1": track}, clips={"r1": clip}, triangulations=tri[:1]))

        got = await db.get_track("T-1")
        assert got["point_count"] == 3 and got["last_point"]["alt_m"] == 9144.0 and got["flags"] == ["hypersonic"]
        assert [t["track_id"] for t in await db.list_tracks(NOW, 10, "hypersonic")] == ["T-1"]
        assert await db.list_tracks(NOW + 1000, 10, None) == []
        points = await db.track_points("T-1", 10)
        assert [p["t_ns"] for p in points] == [NOW, NOW + 100] and points[0]["node_ids"] == ["node-a", "node-b"]
        assert (await db.get_clip("r1")).bytes == 1234
        assert [c.request_id for c in await db.track_clips("T-1")] == ["r1"]
        nodes = await db.list_nodes()
        assert nodes[0].label == "Roof" and nodes[0].status == {"fps": 30}

        conn = await asyncpg.connect(database_url)
        try:
            # The geography column answers metric spatial queries through the GiST index.
            near = await conn.fetchval(
                "SELECT count(*) FROM triangulations WHERE ST_DWithin(position, "
                "ST_SetSRID(ST_MakePoint(-112.2, 33.55), 4326)::geography, 500)")
            assert near == 2
            assert await conn.fetchval("SELECT ST_Z(position::geometry) FROM triangulations LIMIT 1") == 9144.0
            assert await conn.fetchval("SELECT count(*) FROM detections") == 1
        finally:
            await conn.close()
    finally:
        await db.close()


async def test_batch_writer_flushes_on_stop(database_url):
    await migrate(database_url)
    db = Database(database_url)
    await db.start()
    writer = BatchWriter(db, interval_ms=10_000)
    writer.start()
    writer.submit(DbBatch(nodes={"n": NodeRecord("n", last_seen_ns=NOW)}))
    await writer.stop()
    assert [n.node_id for n in await db.list_nodes()] == ["n"]
    await db.close()


async def test_volunteers(database_url):
    await migrate(database_url)
    db = Database(database_url)
    await db.start()
    try:
        base = {"name": None, "region": None, "city": None, "postal_code": None, "approx_lat": None,
                "approx_lon": None, "site_type": None, "has_hardware": None, "interests": [], "notes": None,
                "consent": True, "country": "US"}
        for i in range(3):
            await db.upsert_volunteer({**base, "email": f"v{i}@example.org", "approx_lat": 33.63, "approx_lon": -112.37})
        await db.upsert_volunteer({**base, "email": "lone@example.org", "approx_lat": 40.1, "approx_lon": -100.2})
        await db.upsert_volunteer({**base, "email": "v0@example.org", "name": "Jane", "approx_lat": 33.6,
                                   "approx_lon": -112.3, "interests": ["software"]})  # same email: update
        summary = await db.volunteer_summary(3)
        assert summary == {"total": 4, "cells": [{"lat": 33.5, "lon": -112.5, "count": 3}]}
        rows = await db.list_volunteers()
        assert len(rows) == 4 and rows[0]["name"] == "Jane" and rows[0]["interests"] == ["software"]
        assert rows[0]["updated_at"] >= rows[0]["created_at"]
        assert await db.delete_volunteer(rows[0]["id"])
        assert not await db.delete_volunteer(rows[0]["id"])
        assert (await db.volunteer_summary(3))["cells"] == []
    finally:
        await db.close()
