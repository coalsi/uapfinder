"""The StateBackend contract, run against MemoryState and a real RedisState."""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Optional

import pytest

from skynet_common.geodesy import azel_to_ecef_unit, geodetic_to_ecef
from skynet_server.models import ClipRecord, Commit, Detection, NodeUpdate, TrackState, track_point
from skynet_server.state.base import Admission, StateBackend
from skynet_server.state.memory import MemoryState
from skynet_server.stats import StatsDelta

NOW = time.time_ns()


@pytest.fixture(params=["memory", "redis"])
async def backend(request, redis_prefix) -> AsyncIterator[StateBackend]:
    if request.param == "memory":
        state: StateBackend = MemoryState()
    else:
        from skynet_server.state.redis import RedisState

        state = RedisState(request.getfixturevalue("redis_url"), redis_prefix)
    await state.start()
    yield state
    await state.close()


def update(node_id: str, lat: float, lon: float, t_ns: int = NOW, heartbeat: bool = False) -> NodeUpdate:
    extra = dict(fov_h_deg=90.0, fov_v_deg=60.0, status={"fps": 30}, software_version="t") if heartbeat else {}
    return NodeUpdate(node_id, t_ns, lat, lon, 300.0, 10.0, 45.0, 0.0, **extra)


def detection(node_id: str, t_ns: int, seq: int, track: Optional[str], lat: float = 33.5, lon: float = -112.0,
              az: float = 10.0, el: float = 45.0) -> Detection:
    return Detection(node_id, t_ns, seq, track, lat, lon, 300.0, az, el, geodetic_to_ecef(lat, lon, 300.0),
                     azel_to_ecef_unit(lat, lon, az, el))


async def test_admit_replay_and_node_records(backend: StateBackend):
    items = [Admission(update("a", 33.5, -112.0, heartbeat=True), seq=1),
             Admission(update("a", 33.5, -112.0, NOW + 1), seq=2, detection=detection("a", NOW + 1, 2, "t1")),
             Admission(update("a", 33.5, -112.0, NOW + 1), seq=2)]  # replay of the previous one
    res = await backend.admit(items, NOW)
    assert res.fresh == [True, True, False]
    assert list(res.nodes) == ["a"]
    node = res.nodes["a"]
    assert (node.fov_h_deg, node.status, node.software_version) == (90.0, {"fps": 30}, "t")
    # Same seq with a different timestamp is a different message (e.g. a node restart).
    again = await backend.admit([Admission(update("a", 33.5, -112.0, NOW + 5), seq=2)], NOW)
    assert again.fresh == [True]
    stored = await backend.get_node("a")
    assert stored is not None and stored.lat == pytest.approx(33.5) and stored.last_seen_ns > 0
    assert await backend.count_active_nodes(NOW - 10_000_000_000) == 1
    assert [n.node_id for n in await backend.list_nodes()] == ["a"]


async def test_neighbours_within_radius(backend: StateBackend):
    nodes = {"a": (33.5, -112.0), "b": (33.6, -112.1), "c": (34.0, -111.5), "far": (40.0, -100.0)}
    await backend.admit([Admission(update(n, lat, lon), seq=1) for n, (lat, lon) in nodes.items()], NOW)
    found = await backend.neighbours({"a": nodes["a"]}, 100_000.0, NOW)
    assert found == {"a": ["b", "c"]}
    found = await backend.neighbours({"a": nodes["a"]}, 20_000.0, NOW)
    assert found == {"a": ["b"]}


async def test_redis_neighbours_exclude_inactive(redis_url, redis_prefix):
    from skynet_server.state.redis import RedisState

    state = RedisState(redis_url, redis_prefix)
    await state.start()
    try:
        await state.admit([Admission(update("a", 33.5, -112.0), 1), Admission(update("b", 33.6, -112.0), 1)], NOW)
        later = NOW + 120_000_000_000  # both last seen 2 minutes before "later"
        assert (await state.neighbours({"a": (33.5, -112.0)}, 50_000.0, later)) == {"a": []}
    finally:
        await state.close()


async def test_detection_source(backend: StateBackend):
    dets = [detection("b", NOW + i * 100_000_000, i, "t1") for i in range(5)]
    dets.append(detection("b", NOW + 50_000_000, 99, None))
    items = [Admission(update("b", 33.5, -112.0, d.t_ns), d.seq, d) for d in dets]
    window = {"b": (NOW, NOW + 400_000_000)}
    res = await backend.admit(items, NOW, window)
    samples = res.source.local_track("b", "t1")
    assert samples is not None and samples[0] == [d.t_ns for d in dets[:5]]
    assert samples[1][2].direction == pytest.approx(dets[2].direction)
    assert len(list(res.source.local_tracks("b"))) == 1
    loose = res.source.untracked("b", NOW, NOW + 100_000_000)
    assert [d.seq for d in loose] == [99]
    assert res.source.local_track("nobody", "t1") is None
    # Prefetching backends extend the source on demand.
    extra = detection("b", NOW + 900_000_000, 9, "t1")
    await backend.admit([Admission(update("b", 33.5, -112.0, extra.t_ns), 9, extra)], NOW)
    await backend.fetch_more(res.source, {"b": (NOW, NOW + 1_000_000_000)})
    if backend.prefetch:
        assert res.source.local_track("b", "t1")[0][-1] == extra.t_ns


async def test_claims_expire(backend: StateBackend):
    assert await backend.claim(["k1", "k2"], 200) == [True, True]
    assert await backend.claim(["k1", "k3"], 200) == [False, True]
    await asyncio.sleep(0.3)
    assert await backend.claim(["k1"], 200) == [True]


async def test_tracks_links_points_and_events(backend: StateBackend):
    t1 = TrackState("T-1", NOW, NOW, NOW + 10, node_ids=["a", "b"], point_count=1, classification="hypersonic",
                    recent=[(NOW, 1.0, 2.0, 3.0, 4.0)])
    t2 = TrackState("T-2", NOW, NOW, NOW + 20, node_ids=["c"], point_count=1)
    commit = Commit(
        tracks={"T-1": t1, "T-2": t2},
        new_points={"T-1": [track_point(NOW + 10, 33.5, -112.0, 9000.0, 20.0, 5.0, ["a", "b"])]},
        links={"a|t1": "T-1", "b|t9": "T-1"},
        events=[{"type": "track", "t_ns": NOW, "data": {"n": i}} for i in range(5)],
        commands={"a": ["cmd-1", "cmd-2"]},
    )
    stored = await backend.commit(commit, link_ttl_ms=5_000)
    ids = [e["id"] for e in stored]
    assert len(ids) == 5 and len(set(ids)) == 5
    assert await backend.get_links(["a|t1", "b|t9", "zz|t"]) == {"a|t1": "T-1", "b|t9": "T-1"}
    got = await backend.get_tracks(["T-1", "T-missing"])
    assert list(got) == ["T-1"] and got["T-1"].recent == [(NOW, 1.0, 2.0, 3.0, 4.0)]
    assert [t.track_id for t in await backend.list_tracks(NOW, 10, None)] == ["T-2", "T-1"]
    assert [t.track_id for t in await backend.list_tracks(NOW, 10, "hypersonic")] == ["T-1"]
    assert [t.track_id for t in await backend.list_tracks(NOW + 15, 10, None)] == ["T-2"]
    pts = await backend.get_track_points(["T-1", "T-2"], 200)
    assert pts["T-1"][0]["alt_m"] == 9000.0 and "T-2" not in pts
    assert await backend.count_active_tracks(NOW) == 2

    # Events: newest page, then strictly-after paging.
    latest = await backend.events_after(None, 2)
    assert [e["id"] for e in latest] == ids[-2:]
    page = await backend.events_after(ids[1], 2)
    assert [e["id"] for e in page] == ids[2:4] and page[0]["data"] == {"n": 2}
    assert await backend.events_after(ids[-1], 10) == []
    assert await backend.last_event_id() == ids[-1]
    more = await backend.append_events([{"type": "node", "t_ns": NOW, "data": {}}])
    assert (await backend.events_after(ids[-1], 10))[0]["id"] == more[0]["id"]

    # Commands are handed out once.
    assert await backend.pop_commands(["a", "b"]) == {"a": ["cmd-1", "cmd-2"]}
    assert await backend.pop_commands(["a"]) == {}


async def test_clips_and_registration(backend: StateBackend):
    clip = ClipRecord("r1", "e1", "T-1", "a", NOW - 5, NOW + 5, NOW)
    await backend.commit(Commit(clips=[clip]), 1000)
    assert (await backend.get_clip("r1")) == clip
    clip.status, clip.bytes = "uploaded", 123
    await backend.put_clip(clip)
    assert [(c.request_id, c.status, c.bytes) for c in await backend.track_clips("T-1")] == [("r1", "uploaded", 123)]
    assert await backend.get_clip("nope") is None

    node, created = await backend.register_node("n-1", "Roof", NOW)
    assert created and node.label == "Roof" and node.registered_ns == NOW
    node, created = await backend.register_node("n-1", None, NOW + 1)
    assert not created and node.label == "Roof" and node.registered_ns == NOW
    assert (await backend.get_node("n-1")).last_seen_ns == 0


async def test_rate_limit(backend: StateBackend):
    assert [await backend.rate_limit("ip:1", 2, 3600) for _ in range(3)] == [True, True, False]
    assert await backend.rate_limit("ip:2", 2, 3600)
    assert await backend.rate_limit("ip:3", 1, 0.2)
    await asyncio.sleep(0.3)
    assert await backend.rate_limit("ip:3", 1, 0.2)


async def test_redis_shared_addresses_and_stats(redis_url, redis_prefix):
    from skynet_server.state.redis import RedisState

    state = RedisState(redis_url, redis_prefix)
    await state.start()
    try:
        delta = StatsDelta()
        delta.counters.update({"packets": 10, "matches": 2})
        delta.rejected.update({"stale": 3})
        delta.per_second[int(time.time())] = delta.counters.copy()
        delta.processing_ms.extend([1.0, 2.0, 3.0])
        await state.commit(Commit(udp_addrs={"a": (("10.0.0.1", 5000), NOW)}), 1000, delta)
        await state.commit(Commit(), 1000, delta)
        assert await state.get_udp_addrs(["a", "b"]) == {"a": (("10.0.0.1", 5000), NOW)}
        snap = await state.shared_stats()
        assert snap["packets_total"] == 20 and snap["rejected_total"] == {"stale": 6}
        assert snap["packets_per_s"] > 0 and snap["processing_latency_ms"]["max"] == 3.0
    finally:
        await state.close()
