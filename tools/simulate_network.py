#!/usr/bin/env python3
"""Simulate a SKY-NET node network against a running server and score it.

Virtual edge nodes observe synthetic targets with known 3D trajectories,
send signed heartbeats and telemetry exactly as real nodes would (UDP or the
HTTPS NDJSON ingest), listen for clip_request back-signals, and at the end
compare the server's triangulated tracks with the ground truth.

The simulated nodes stand in for the output of the edge pipeline *after*
its false-positive filters, so every visible target is reported.

Examples:
    # Standalone server with UDP ingest on localhost
    python tools/simulate_network.py --udp 127.0.0.1:9999 --api http://127.0.0.1:8000 \
        --master-key "$SKYNET_MASTER_KEY" --duration 30

    # HTTPS ingest (e.g. a Vercel deployment)
    python tools/simulate_network.py --https https://example.vercel.app \
        --master-key "$SKYNET_MASTER_KEY" --duration 30

The master key is needed to derive node keys; use a development key, never
production credentials, on shared machines.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import socket
import statistics
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from skynet_common import geodesy as g  # noqa: E402
from skynet_common.camera import PinholeCamera, azel_to_pixel  # noqa: E402
from skynet_common.protocol import (  # noqa: E402
    ProtocolError,
    decode_for_node,
    derive_node_key,
    encode_signed,
)

Vec3 = Tuple[float, float, float]


# --- scenario ----------------------------------------------------------------


@dataclass
class SimNode:
    node_id: str
    lat: float
    lon: float
    alt_m: float
    yaw_deg: float
    pitch_deg: float
    roll_deg: float = 0.0
    camera: PinholeCamera = field(default_factory=lambda: PinholeCamera.from_fov(1920, 1080, 90.0))
    key: bytes = b""
    sock: Optional[socket.socket] = None
    seq: int = 0
    clip_requests: List[Tuple[float, str]] = field(default_factory=list)  # (receive time, event id)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


@dataclass
class Target:
    name: str
    # ENU offset in metres from the scenario centre as a function of time (s).
    enu_at: Callable[[float], Vec3]
    expect_flag: Optional[str] = None


def straight(start: Vec3, velocity: Vec3) -> Callable[[float], Vec3]:
    def f(t: float) -> Vec3:
        return (start[0] + velocity[0] * t, start[1] + velocity[1] * t, start[2] + velocity[2] * t)

    return f


def hover_then_dart(hover: Vec3, t_turn: float, accel: Vec3) -> Callable[[float], Vec3]:
    """Hold position, then accelerate hard in a fixed direction (non-ballistic)."""

    def f(t: float) -> Vec3:
        if t < t_turn:
            return hover
        dt = t - t_turn
        return (
            hover[0] + 0.5 * accel[0] * dt * dt,
            hover[1] + 0.5 * accel[1] * dt * dt,
            hover[2] + 0.5 * accel[2] * dt * dt,
        )

    return f


def build_scenario(center: Tuple[float, float], n_nodes: int, rng: random.Random) -> Tuple[List[SimNode], List[Target]]:
    """Nodes on a ring 15-35 km from the centre, cameras aimed at the shared airspace."""
    lat0, lon0 = center
    nodes = []
    for i in range(n_nodes):
        bearing = 360.0 * i / n_nodes + rng.uniform(-10, 10)
        dist = rng.uniform(15_000, 35_000)
        e, n = dist * math.sin(math.radians(bearing)), dist * math.cos(math.radians(bearing))
        lat, lon, _ = enu_to_geodetic(center, (e, n, 0.0))
        alt = 330.0 + rng.uniform(-20, 60)
        # Point back towards the centre, 35-55 degrees up.
        yaw = (bearing + 180.0) % 360.0
        nodes.append(SimNode(f"sim-node-{i:02d}", lat, lon, alt, yaw, rng.uniform(35, 55), rng.uniform(-2, 2)))
    targets = [
        Target("hypersonic", straight((-30_000.0, -5_000.0, 20_000.0), (2_000.0, 300.0, 0.0)), "hypersonic"),
        Target("hover-dart", hover_then_dart((3_000.0, 2_000.0, 6_000.0), 8.0, (-150.0, 120.0, 40.0)), "high_g"),
        Target("cruise", straight((10_000.0, -15_000.0, 9_000.0), (-120.0, 180.0, 0.0))),
    ]
    return nodes, targets


def enu_to_geodetic(center: Tuple[float, float], enu: Vec3) -> Vec3:
    lat0, lon0 = center
    origin = g.geodetic_to_ecef(lat0, lon0, 0.0)
    d = g.enu_to_ecef_vector(lat0, lon0, enu)
    return g.ecef_to_geodetic(*g.add(origin, d))


# --- transports ----------------------------------------------------------------


class UdpTransport:
    def __init__(self, host: str, port: int, nodes: List[SimNode]):
        self.addr = (host, port)
        self.nodes = nodes
        self._stop = threading.Event()
        for node in nodes:
            node.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            node.sock.bind(("0.0.0.0", 0))
            node.sock.settimeout(0.2)
        self.threads = [threading.Thread(target=self._listen, args=(n,), daemon=True) for n in nodes]
        for t in self.threads:
            t.start()

    def send(self, node: SimNode, data: bytes) -> None:
        assert node.sock is not None
        node.sock.sendto(data, self.addr)

    def flush(self) -> None:
        pass

    def _listen(self, node: SimNode) -> None:
        assert node.sock is not None
        while not self._stop.is_set():
            try:
                data, _ = node.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            handle_command(node, data)

    def close(self) -> None:
        self._stop.set()
        for t in self.threads:
            t.join(timeout=1)
        for node in self.nodes:
            if node.sock:
                node.sock.close()


class HttpsTransport:
    """Batches NDJSON lines and POSTs them every flush (10 Hz)."""

    def __init__(self, base_url: str, nodes: List[SimNode]):
        self.url = base_url.rstrip("/") + "/api/v1/ingest"
        self.by_id = {n.node_id: n for n in nodes}
        self.lines: List[bytes] = []
        self.rejected: Dict[str, int] = {}

    def send(self, node: SimNode, data: bytes) -> None:
        self.lines.append(data)

    def flush(self) -> None:
        if not self.lines:
            return
        body, self.lines = b"\n".join(self.lines), []
        req = urllib.request.Request(self.url, data=body, method="POST", headers={"Content-Type": "application/x-ndjson"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read())
        for r in out.get("rejected", []):
            self.rejected[r["reason"]] = self.rejected.get(r["reason"], 0) + 1
        for cmd in out.get("commands", []):
            raw = cmd.encode()
            node_id = json.loads(raw).get("node_id")
            if node_id in self.by_id:
                handle_command(self.by_id[node_id], raw)

    def close(self) -> None:
        self.flush()


def handle_command(node: SimNode, data: bytes) -> None:
    try:
        msg = decode_for_node(data, node.key)
    except ProtocolError as exc:
        print(f"[{node.node_id}] rejected server message: {exc}", file=sys.stderr)
        return
    node.clip_requests.append((time.time(), getattr(msg, "event_id", "")))


# --- messages ------------------------------------------------------------------


def heartbeat(node: SimNode, now_ns: int) -> dict:
    cam = node.camera
    return {
        "type": "heartbeat",
        "node_id": node.node_id,
        "timestamp_ns": now_ns,
        "seq": node.next_seq(),
        "gps_coordinates": {"latitude": node.lat, "longitude": node.lon, "altitude_m": node.alt_m},
        "imu_orientation": {"pitch_deg": node.pitch_deg, "roll_deg": node.roll_deg, "yaw_deg": node.yaw_deg},
        "camera": {"width": cam.width, "height": cam.height, "fps": 60.0, "fov_h_deg": cam.fov_h_deg, "fov_v_deg": cam.fov_v_deg},
        "status": {"fps": 60.0, "mem_used_mb": 3000, "tracks_active": 0, "simulated": True},
        "software_version": "sim-0.1.0",
    }


def telemetry(node: SimNode, track_id: str, t_ns: int, az: float, el: float, ang_vel: float) -> dict:
    return {
        "type": "telemetry",
        "node_id": node.node_id,
        "gps_coordinates": {"latitude": node.lat, "longitude": node.lon, "altitude_m": node.alt_m},
        "imu_orientation": {"pitch_deg": node.pitch_deg, "roll_deg": node.roll_deg, "yaw_deg": node.yaw_deg},
        "timestamp_ns": t_ns,
        "vector_target": {"azimuth_deg": round(az % 360.0, 6), "elevation_deg": round(el, 6)},
        "pixel_metrics": {"angular_velocity_deg_s": round(ang_vel, 4), "relative_intensity": 0.8},
        "seq": node.next_seq(),
        "track_id": track_id,
        "anomaly_score": 0.5,
    }


# --- run -------------------------------------------------------------------------


def visible(node: SimNode, az: float, el: float, rng_m: float, min_el: float = 5.0, max_range_m: float = 200_000.0) -> bool:
    if el < min_el or rng_m > max_range_m:
        return False
    px = azel_to_pixel(node.camera, az, el, node.yaw_deg, node.pitch_deg, node.roll_deg)
    return px is not None and node.camera.in_image(*px)


def run(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    center = (args.lat, args.lon)
    nodes, targets = build_scenario(center, args.nodes, rng)
    master = args.master_key.encode()
    for node in nodes:
        node.key = derive_node_key(master, node.node_id)

    if args.udp:
        host, port = args.udp.rsplit(":", 1)
        transport = UdpTransport(host, int(port), nodes)
    else:
        transport = HttpsTransport(args.https, nodes)

    # Like real nodes, local track ids are unique per run, so a new run is
    # never linked to a previous run's 3D tracks on a long-lived server.
    run_id = format(int(time.time()), "x")
    truth: Dict[str, List[Tuple[int, Vec3]]] = {t.name: [] for t in targets}
    prev_dir: Dict[Tuple[str, int], Tuple[float, Vec3]] = {}
    sent = 0
    t0 = time.time()
    t0_ns = time.time_ns()
    next_hb = 0.0
    period = 1.0 / args.report_hz
    tick = 0
    try:
        while True:
            elapsed = time.time() - t0
            if elapsed >= args.duration:
                break
            now_ns = time.time_ns()
            if elapsed >= next_hb:
                for node in nodes:
                    transport.send(node, encode_signed(heartbeat(node, now_ns), node.key))
                next_hb += 10.0
            t_sim = (now_ns - t0_ns) / 1e9
            for ti, target in enumerate(targets):
                lat, lon, alt = enu_to_geodetic(center, target.enu_at(t_sim))
                truth[target.name].append((now_ns, g.geodetic_to_ecef(lat, lon, alt)))
                for node in nodes:
                    az, el, rng_m = g.look_angles(node.lat, node.lon, node.alt_m, lat, lon, alt)
                    if not visible(node, az, el, rng_m):
                        continue
                    # Each node's frame clock jitters by a few ms around the true time.
                    jitter_ns = int(rng.gauss(0, args.timing_jitter_ms) * 1e6)
                    az_n = az + rng.gauss(0, args.angular_sigma_deg) / max(math.cos(math.radians(el)), 0.05)
                    el_n = el + rng.gauss(0, args.angular_sigma_deg)
                    d = g.azel_to_enu(az, el)
                    key = (node.node_id, ti)
                    ang_vel = 0.0
                    if key in prev_dir:
                        pt, pd = prev_dir[key]
                        ang_vel = g.angle_between_deg(pd, d) / max(t_sim - pt, 1e-3)
                    prev_dir[key] = (t_sim, d)
                    pkt = telemetry(node, f"{run_id}-t{ti}", now_ns + jitter_ns, az_n, el_n, ang_vel)
                    transport.send(node, encode_signed(pkt, node.key))
                    sent += 1
            transport.flush()
            tick += 1
            sleep = t0 + tick * period - time.time()
            if sleep > 0:
                time.sleep(sleep)
    finally:
        time.sleep(1.0)  # let late back-signals arrive
        transport.close()

    print(f"sent {sent} telemetry messages from {len(nodes)} nodes over {args.duration:.0f} s")
    if isinstance(transport, HttpsTransport) and transport.rejected:
        print(f"rejected by server: {transport.rejected}")
    clip_nodes = sum(1 for n in nodes if n.clip_requests)
    print(f"clip_request back-signals received by {clip_nodes}/{len(nodes)} nodes "
          f"({sum(len(n.clip_requests) for n in nodes)} total)")
    if not args.api:
        return 0
    return score(args.api, truth, targets, since_ns=t0_ns)


def nearest_truth(series: List[Tuple[int, Vec3]], t_ns: int) -> Tuple[int, Vec3]:
    lo, hi = 0, len(series) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] < t_ns:
            lo = mid + 1
        else:
            hi = mid
    candidates = [series[i] for i in (lo - 1, lo) if 0 <= i < len(series)]
    return min(candidates, key=lambda s: abs(s[0] - t_ns))


def score(api: str, truth: Dict[str, List[Tuple[int, Vec3]]], targets: List[Target], since_ns: int) -> int:
    with urllib.request.urlopen(f"{api.rstrip('/')}/api/v1/tracks?since_ns={since_ns}&limit=1000", timeout=10) as resp:
        tracks = json.loads(resp.read())["tracks"]
    print(f"server reported {len(tracks)} tracks")
    failures = 0
    assigned: Dict[str, List[dict]] = {t.name: [] for t in targets}
    for tr in tracks:
        with urllib.request.urlopen(f"{api.rstrip('/')}/api/v1/tracks/{tr['track_id']}", timeout=10) as resp:
            full = json.loads(resp.read())
        pts = full.get("points", [])
        if not pts:
            continue
        # Assign each server track to the target whose truth it is closest to.
        def mean_err(name: str) -> float:
            errs = []
            for p in pts:
                _, tp = nearest_truth(truth[name], p["t_ns"])
                errs.append(g.norm(g.sub(g.geodetic_to_ecef(p["lat"], p["lon"], p["alt_m"]), tp)))
            return statistics.mean(errs)

        best = min(truth, key=mean_err)
        assigned[best].append(full)

    for target in targets:
        group = assigned[target.name]
        if not group:
            print(f"  {target.name:<11} NOT TRACKED")
            failures += 1
            continue
        errs, sigmas = [], []
        for tr in group:
            for p in tr["points"]:
                _, tp = nearest_truth(truth[target.name], p["t_ns"])
                errs.append(g.norm(g.sub(g.geodetic_to_ecef(p["lat"], p["lon"], p["alt_m"]), tp)))
                sigmas.append(p.get("sigma_m") or float("nan"))
        errs.sort()
        flags = sorted({f for tr in group for f in tr.get("flags", [])})
        classes = sorted({tr.get("classification") for tr in group})
        p95 = errs[min(len(errs) - 1, int(0.95 * len(errs)))]
        print(
            f"  {target.name:<11} tracks={len(group)} points={len(errs)} "
            f"err median={statistics.median(errs):.0f} m p95={p95:.0f} m "
            f"median sigma={statistics.median(sigmas):.0f} m class={classes} flags={flags}"
        )
        if target.expect_flag and target.expect_flag not in flags:
            print(f"    expected flag {target.expect_flag!r} was not raised")
            failures += 1
    return 1 if failures else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    dest = ap.add_mutually_exclusive_group(required=True)
    dest.add_argument("--udp", help="server UDP endpoint host:port")
    dest.add_argument("--https", help="server base URL for NDJSON ingest")
    ap.add_argument("--api", help="server base URL for scoring (GET /api/v1/tracks); defaults to --https")
    ap.add_argument("--master-key", required=True, help="server SKYNET_MASTER_KEY (development key)")
    ap.add_argument("--nodes", type=int, default=6)
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--report-hz", type=float, default=10.0)
    ap.add_argument("--angular-sigma-deg", type=float, default=0.03)
    ap.add_argument("--timing-jitter-ms", type=float, default=2.0)
    ap.add_argument("--lat", type=float, default=33.55)
    ap.add_argument("--lon", type=float, default=-112.20)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    if args.https and not args.api:
        args.api = args.https
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
