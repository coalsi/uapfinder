"""Clip-request back-signals over a real local UDP socket."""

from __future__ import annotations

import asyncio
import socket
import time

from skynet_common.geodesy import look_angles
from skynet_common.protocol import ClipRequest, ProtocolError, decode_for_node
from skynet_server import sim
from skynet_server.engine import Engine, Inbound
from skynet_server.live import EventHub
from skynet_server.state.memory import MemoryState
from skynet_server.stats import Stats
from skynet_server.udp import UdpListener

from .conftest import MASTER_KEY, make_settings

UDP_PORT = 9101
TARGET = (33.55, -112.20, 9000.0)


async def receive(sock: socket.socket, timeout: float = 2.0) -> bytes:
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.sock_recv(sock, 4096), timeout)


async def test_clip_request_reaches_each_node_once():
    settings = make_settings(public_url="http://skynet.example:8000")
    engine = Engine(settings, MemoryState(), Stats(), EventHub())
    listener = UdpListener(engine, "127.0.0.1", UDP_PORT, batch_max=16)
    await listener.start()
    nodes = []
    for node_id, lat, lon in (("node-a", 33.40, -112.35), ("node-b", 33.72, -112.05)):
        az, el, _ = look_angles(lat, lon, 300.0, *TARGET)
        nodes.append(sim.SimNode(node_id, lat, lon, 300.0, yaw_deg=az, pitch_deg=el))
    network = sim.SimNetwork(MASTER_KEY, nodes)
    socks = {}
    try:
        for node in network.nodes:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            s.setblocking(False)
            socks[node.node_id] = s
            s.sendto(network.sign(sim.heartbeat_dict(node, time.time_ns())), ("127.0.0.1", UDP_PORT))
        now = time.time_ns()
        for frame in range(3):
            for node in network.nodes:
                az, el, _ = look_angles(node.lat, node.lon, node.alt_m, *TARGET)
                msg = sim.telemetry_dict(node, now + frame * 100_000_000, az, el, "t1")
                socks[node.node_id].sendto(network.sign(msg), ("127.0.0.1", UDP_PORT))
            await asyncio.sleep(0.05)
        for node in network.nodes:
            raw = await receive(socks[node.node_id])
            req = decode_for_node(raw, network.key(node.node_id))
            assert isinstance(req, ClipRequest) and req.node_id == node.node_id
            assert req.upload_url == f"http://skynet.example:8000/api/v1/clips/{req.request_id}/upload-target"
            # Signed with this node's key only: the other node must reject it.
            other = next(n for n in network.nodes if n.node_id != node.node_id)
            try:
                decode_for_node(raw, network.key(other.node_id))
                raise AssertionError("a clip request verified with another node's key")
            except ProtocolError as exc:
                assert exc.reason == "bad_signature"
        # One request per (track, node): later frames send nothing more.
        for s in socks.values():
            try:
                await receive(s, timeout=0.3)
                raise AssertionError("duplicate clip request")
            except asyncio.TimeoutError:
                pass
        assert engine.stats.counters["clip_requests_udp"] == 2
        latencies = engine.stats.backsignal.samples()
        assert len(latencies) == 2 and max(latencies) < 20.0  # ms from receipt to sendto
    finally:
        await listener.stop()
        for s in socks.values():
            s.close()


async def test_node_that_switched_to_https_gets_commands_not_udp():
    """A node seen over UDP that now reports over HTTPS must not have its clip
    request sent to the stale UDP address."""
    settings = make_settings(public_url="http://skynet.example:8000")
    engine = Engine(settings, MemoryState(), Stats(), EventHub())
    listener = UdpListener(engine, "127.0.0.1", UDP_PORT + 1, batch_max=16)
    await listener.start()
    nodes = []
    for node_id, lat, lon in (("node-a", 33.40, -112.35), ("node-b", 33.72, -112.05)):
        az, el, _ = look_angles(lat, lon, 300.0, *TARGET)
        nodes.append(sim.SimNode(node_id, lat, lon, 300.0, yaw_deg=az, pitch_deg=el))
    network = sim.SimNetwork(MASTER_KEY, nodes)
    socks = {}
    try:
        for node in network.nodes:  # first contact over UDP records a return address
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            s.setblocking(False)
            socks[node.node_id] = s
            s.sendto(network.sign(sim.heartbeat_dict(node, time.time_ns())), ("127.0.0.1", UDP_PORT + 1))
        await asyncio.sleep(0.1)
        now = time.time_ns()
        items = []
        for frame in range(3):
            for node in network.nodes:
                az, el, _ = look_angles(node.lat, node.lon, node.alt_m, *TARGET)
                msg = sim.telemetry_dict(node, now + frame * 100_000_000, az, el, "t1")
                items.append(Inbound(network.sign(msg), time.time_ns(), time.perf_counter_ns()))
        await engine.process(items)  # HTTPS ingest: no source address
        assert engine.stats.counters.get("clip_requests_udp", 0) == 0
        assert engine.stats.counters["clip_requests_queued"] == 2
        queued = await engine.state.pop_commands(["node-a", "node-b"])
        assert sorted(queued) == ["node-a", "node-b"]
        for s in socks.values():
            try:
                await receive(s, timeout=0.3)
                raise AssertionError("clip request sent to the stale UDP address")
            except asyncio.TimeoutError:
                pass
    finally:
        await listener.stop()
        for s in socks.values():
            s.close()
