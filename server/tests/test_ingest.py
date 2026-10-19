"""The ingest pipeline (Engine.process) against both state backends."""

from __future__ import annotations

import time
from typing import AsyncIterator, List

import pytest

from skynet_common.geodesy import look_angles
from skynet_common.protocol import ClipRequest, decode_for_node, encode_signed
from skynet_server import sim
from skynet_server.engine import Engine, Inbound
from skynet_server.live import EventHub
from skynet_server.state.memory import MemoryState
from skynet_server.stats import Stats

from .conftest import MASTER_KEY, make_settings

TARGET = (33.55, -112.20, 9000.0)


def aimed(node_id: str, lat: float, lon: float) -> sim.SimNode:
    """A node whose camera points at TARGET (so the target is in its view)."""
    az, el, _ = look_angles(lat, lon, 300.0, *TARGET)
    return sim.SimNode(node_id, lat, lon, 300.0, yaw_deg=az, pitch_deg=el)


NODES = [aimed("node-a", 33.40, -112.35), aimed("node-b", 33.72, -112.05), aimed("node-c", 33.70, -112.40)]


@pytest.fixture(params=["memory", "redis"])
async def engine(request, redis_prefix) -> AsyncIterator[Engine]:
    settings = make_settings()
    if request.param == "memory":
        state = MemoryState()
    else:
        from skynet_server.state.redis import RedisState

        state = RedisState(request.getfixturevalue("redis_url"), redis_prefix)
    await state.start()
    yield Engine(settings, state, Stats(shared=request.param == "redis"), EventHub())
    await state.close()


def net() -> sim.SimNetwork:
    return sim.SimNetwork(MASTER_KEY, [aimed(n.node_id, n.lat, n.lon) for n in NODES])


def inbound(*datas: bytes, addr=None) -> List[Inbound]:
    now, perf = time.time_ns(), time.perf_counter_ns()
    return [Inbound(d, now, perf, addr) for d in datas]


def sighting(network: sim.SimNetwork, node: sim.SimNode, t_ns: int, track: str = "t1") -> bytes:
    az, el, _ = look_angles(node.lat, node.lon, node.alt_m, *TARGET)
    return network.sign(sim.telemetry_dict(node, t_ns, az, el, track))


async def test_rejections_are_counted(engine: Engine):
    network = net()
    a = network.nodes[0]
    now = time.time_ns()
    good = sighting(network, a, now)
    items = inbound(
        good,
        good,  # replay
        sighting(network, a, now - 30_000_000_000),  # stale
        encode_signed(sim.telemetry_dict(a, now, 1.0, 2.0, "t"), b"x" * 32),  # wrong key
        encode_signed(sim.telemetry_dict(a, now, 1.0, 2.0, "t"), None),  # unsigned
        b"{oops",
        b"{" + b" " * 1300 + b"}",
    )
    out = await engine.process(items)
    assert out.reasons == [None, "replay", "stale", "bad_signature", "unsigned", "bad_json", "too_large"]
    assert out.accepted == 1 and out.node_ids == {"node-a"}
    assert dict(engine.stats.rejected) == {"replay": 1, "stale": 1, "bad_signature": 1, "unsigned": 1,
                                           "bad_json": 1, "too_large": 1}


async def test_heartbeat_updates_node_and_emits_event(engine: Engine):
    network = net()
    seen: list = []
    engine.hub.publish = seen.extend  # type: ignore[method-assign]
    await engine.process(inbound(network.sign(sim.heartbeat_dict(network.nodes[0], time.time_ns()))))
    node = await engine.state.get_node("node-a")
    assert node.fov_h_deg == 90.0 and node.software_version == "sim"
    assert [e["type"] for e in seen] == ["node"] and seen[0]["data"]["lat"] == round(33.40, 2)


async def test_simultaneous_pair_triangulates_once_and_requests_clips(engine: Engine):
    network = net()
    now = time.time_ns()
    await engine.process(inbound(*(network.sign(sim.heartbeat_dict(n, now)) for n in network.nodes)))
    a, b, _ = network.nodes
    events: list = []
    engine.hub.publish = events.extend  # type: ignore[method-assign]
    out = await engine.process(inbound(sighting(network, a, now), sighting(network, b, now)))
    assert out.matches == 1
    tri = [e for e in events if e["type"] == "triangulation"]
    assert len(tri) == 1 and abs(tri[0]["data"]["alt_m"] - TARGET[2]) < 50
    assert {r["node_id"] for r in tri[0]["data"]["rays"]} == {"node-a", "node-b"}
    track_id = tri[0]["data"]["track_id"]
    # No UDP socket in this test: the back-signals are queued for the HTTPS command path.
    cmds = await engine.state.pop_commands(["node-a", "node-b"])
    assert set(cmds) == {"node-a", "node-b"}
    for node_id, (raw,) in cmds.items():
        req = decode_for_node(raw.encode(), network.key(node_id))
        assert isinstance(req, ClipRequest) and req.node_id == node_id
        assert req.t_end_ns - req.t_start_ns == 2 * engine.settings.clip_window_ns
        assert req.upload_url.endswith(f"/api/v1/clips/{req.request_id}/upload-target")
        assert (await engine.state.get_clip(req.request_id)).track_id == track_id
    assert engine.stats.backsignal.samples()  # latency recorded at enqueueing
    # The next frame joins the same track and asks nobody again.
    later = now + 100_000_000
    await engine.process(inbound(sighting(network, a, later)))
    out = await engine.process(inbound(sighting(network, b, later)))
    assert out.matches == 1
    assert await engine.state.pop_commands(["node-a", "node-b"]) == {}
    # A third node joining the track gets its own request.
    await engine.process(inbound(sighting(network, network.nodes[2], later + 1_000_000)))
    assert list(await engine.state.pop_commands(["node-a", "node-b", "node-c"])) == ["node-c"]
    tracks = await engine.state.list_tracks(0, 10, None)
    assert [t.track_id for t in tracks] == [track_id] and set(tracks[0].node_ids) == {"node-a", "node-b", "node-c"}


async def test_replay_from_another_address_does_not_move_the_return_address(engine: Engine):
    network = net()
    hb = network.sign(sim.heartbeat_dict(network.nodes[0], time.time_ns()))
    await engine.process(inbound(hb, addr=("10.0.0.1", 4000)))
    await engine.process(inbound(hb, addr=("6.6.6.6", 666)))
    assert engine._udp_book["node-a"][0] == ("10.0.0.1", 4000)


async def test_simulated_network_tracks_every_object(engine: Engine):
    """A small but realistic sky: 3 objects over 8 nodes for 3 s at 10 Hz."""
    import random

    rng = random.Random(4)
    t0 = time.time_ns() - 3_000_000_000
    nodes = sim.cluster("n", (33.5, -112.0), 8, 25_000, rng)
    objects = [sim.SimObject(f"o{i}", (33.5 + rng.uniform(-0.05, 0.05), -112.0 + rng.uniform(-0.05, 0.05),
                                       rng.uniform(5000, 12000)), (rng.uniform(-300, 300), rng.uniform(-300, 300),
                                                                   0.0), t0_ns=t0) for i in range(3)]
    network = sim.SimNetwork(MASTER_KEY, nodes, objects, angular_noise_deg=0.02)
    await engine.process(inbound(*(network.sign(sim.heartbeat_dict(n, time.time_ns())) for n in nodes)))
    for f in range(30):
        frame = [network.sign(m) for _, m in network.frame(t0 + f * 100_000_000, jitter_ns=3_000_000)]
        rng.shuffle(frame)
        for i in range(0, len(frame), 4):
            await engine.process(inbound(*frame[i:i + 4]))
    tracks = await engine.state.list_tracks(0, 100, None)
    seen_objects = {m["track_id"].split("@")[0] for _, m in network.frame(t0)}
    assert len(tracks) == len(seen_objects) == 3
    for t in tracks:
        lp = t.last_point
        errors = []
        for obj in objects:
            lat, lon, alt = obj.position(lp["t_ns"])
            errors.append(abs(lat - lp["lat"]) * 111_000 + abs(alt - lp["alt_m"]))
        assert min(errors) < 300.0


async def test_public_rays_are_recomputed_from_rounded_positions(engine: Engine):
    """Published rays must not be the measured ones: back-projected through
    their points they would locate the cameras despite the rounding."""
    nodes = []
    for node_id, lat, lon in (("node-x", 33.40371, -112.35189), ("node-y", 33.71634, -112.04718)):
        az, el, _ = look_angles(lat, lon, 357.2, *TARGET)
        nodes.append(sim.SimNode(node_id, lat, lon, 357.2, yaw_deg=az, pitch_deg=el))
    network = sim.SimNetwork(MASTER_KEY, nodes)
    events: list = []
    engine.hub.publish = events.extend  # type: ignore[method-assign]
    now = time.time_ns()
    await engine.process(inbound(*(network.sign(sim.heartbeat_dict(n, now)) for n in nodes)))
    await engine.process(inbound(*(sighting(network, n, now) for n in nodes)))
    node_events = [e["data"] for e in events if e["type"] == "node"]
    assert {(n["lat"], n["lon"], n["alt_m"]) for n in node_events} == {(33.4, -112.35, 360.0), (33.72, -112.05, 360.0)}
    tri = next(e["data"] for e in events if e["type"] == "triangulation")
    by_id = {n.node_id: n for n in nodes}
    for ray in tri["rays"]:
        node = by_id[ray["node_id"]]
        assert (ray["node_lat"], ray["node_lon"], ray["node_alt_m"]) == (round(node.lat, 2), round(node.lon, 2), 360.0)
        az, el, _ = look_angles(ray["node_lat"], ray["node_lon"], ray["node_alt_m"], tri["lat"], tri["lon"], tri["alt_m"])
        assert (ray["az_deg"], ray["el_deg"]) == (round(az, 4), round(el, 4))
        measured_az, measured_el, _ = look_angles(node.lat, node.lon, node.alt_m, *TARGET)
        assert abs(ray["az_deg"] - measured_az) + abs(ray["el_deg"] - measured_el) > 0.01
