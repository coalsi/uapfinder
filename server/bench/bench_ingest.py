#!/usr/bin/env python3
"""Ingest benchmark: a dense simulated network blasted at a standalone server.

Scenario (deterministic per --seed): 200 nodes in four 40 km clusters around
Phoenix, Flagstaff, Tucson and Prescott, cameras at random azimuths and
30-75 degree elevation (90 x 59 degree FOV), and enough moving objects at
2-18 km altitude (30 m/s to 2.2 km/s) that the nodes together report
``--rate`` messages per second at 10 Hz. Objects live 12 s each, with births
spread over the run, so new tracks (and clip requests) keep appearing. Which node sees which object comes
from real geometry (WGS84, pinhole camera model, 120 km range, 5 degree
minimum elevation), so the pair structure is what a real network would
produce: mostly 2-10 observers per object.

Timing: capture timestamps are PPS-aligned frame times (10 Hz, +-1 ms
jitter); each node reports a frame 5-95 ms after capture, so the server sees
a smooth stream rather than synchronised bursts. Every packet is generated
and signed before the blast, by ``--senders`` processes that each own a
share of the nodes with one UDP socket per node (so return addresses and
SO_REUSEPORT worker affinity behave like real nodes) and record the
clip_request back-signals they receive.

After ``--warmup`` seconds of traffic (a cold server has no search partners
yet, so its first second is a burst of full searches) the run is measured:
achieved ingest rate and loss (telemetry the server never counted), the
server-side processing and back-signal latency percentiles (``/stats``, last
30 s), the
client-side end-to-end back-signal latency (from sending the packet that
completed a match to receiving the clip_request), matches, tracks and server
CPU use (Linux).

    python server/bench/bench_ingest.py --backend memory
    python server/bench/bench_ingest.py --backend redis --workers 3
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import os
import random
import selectors
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "common")]

from skynet_common.camera import camera_basis_enu  # noqa: E402
from skynet_common.geodesy import WGS84_A, WGS84_E2, enu_basis, geodetic_to_ecef  # noqa: E402
from skynet_common.protocol import derive_node_key, encode_signed  # noqa: E402

CLUSTERS = [(33.45, -112.07), (35.20, -111.65), (32.25, -110.95), (34.55, -112.47)]
NODES_PER_CLUSTER = 50
CLUSTER_RADIUS_M = 40_000.0
HZ = 10
MASTER_KEY = "bench-master-key-0123456789abcdef0123456789"
MAX_RANGE_M = 120_000.0
MIN_EL_DEG = 5.0
NOISE_DEG = 0.03
OBJECT_LIFE_S = 12.0


# --- scenario ----------------------------------------------------------------------------


class Scenario:
    def __init__(self, rate: float, seed: int, duration: float):
        rng = random.Random(seed)
        nodes = []
        for ci, (clat, clon) in enumerate(CLUSTERS):
            for i in range(NODES_PER_CLUSTER):
                r, th = CLUSTER_RADIUS_M * math.sqrt(rng.random()), rng.uniform(0, 2 * math.pi)
                lat = clat + r * math.cos(th) / 111_320.0
                lon = clon + r * math.sin(th) / (111_320.0 * math.cos(math.radians(clat)))
                nodes.append((f"c{ci}-n{i:03d}", lat, lon, rng.uniform(300.0, 2200.0), rng.uniform(0, 360),
                              rng.uniform(30, 75)))
        self.node_ids = [n[0] for n in nodes]
        self.node_llh = np.array([[n[1], n[2], n[3]] for n in nodes])
        self.node_att = [(n[4], n[5]) for n in nodes]
        self.node_ecef = np.array([geodetic_to_ecef(*n[1:4]) for n in nodes])
        self.enu = np.array([enu_basis(n[1], n[2]) for n in nodes])  # (N, 3 axes, xyz)
        cams = [camera_basis_enu(yaw, pitch, 0.0) for yaw, pitch in self.node_att]  # right, down, forward in ENU
        self.cam = np.array(cams)  # (N, 3, 3) rows: right, down, forward (ENU components)
        self.tan_h, self.tan_v = math.tan(math.radians(45.0)), math.tan(math.radians(58.7 / 2))
        # Objects live for OBJECT_LIFE_S with births spread over the run, so
        # tracks (and their clip requests) are created all the time rather
        # than in one cold-start burst. Objects are added until the network
        # reports about rate/HZ messages per frame on average.
        self.obj_origin: List[np.ndarray] = []
        self.obj_vel: List[np.ndarray] = []
        self.obj_birth: List[float] = []
        target = rate / HZ
        probes = [duration * i / 4 for i in range(4)]
        while True:
            ci = rng.randrange(len(CLUSTERS))
            clat, clon = CLUSTERS[ci]
            r, th = 1.2 * CLUSTER_RADIUS_M * math.sqrt(rng.random()), rng.uniform(0, 2 * math.pi)
            lat = clat + r * math.cos(th) / 111_320.0
            lon = clon + r * math.sin(th) / (111_320.0 * math.cos(math.radians(clat)))
            speed, heading = rng.choice([30.0, 150.0, 250.0, 900.0, 2200.0]), rng.uniform(0, 2 * math.pi)
            e, n, u = enu_basis(lat, lon)
            v = speed * (math.sin(heading) * np.array(e) + math.cos(heading) * np.array(n))
            birth = rng.uniform(-OBJECT_LIFE_S, duration)
            # Positions are given relative to mid-life, so the object crosses
            # the cluster's sky while it is alive.
            self.obj_origin.append(np.array(geodetic_to_ecef(lat, lon, rng.uniform(2000.0, 18000.0)))
                                   - v * (birth + OBJECT_LIFE_S / 2))
            self.obj_vel.append(v)
            self.obj_birth.append(birth)
            if len(self.obj_origin) % 10 == 0:
                if np.mean([self.visible(t)[0].sum() for t in probes]) >= target:
                    break
        self.objects = len(self.obj_origin)

    def visible(self, t_s: float, nodes: Optional[np.ndarray] = None) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(mask, az_deg, el_deg), each (nodes, objects), for objects at time t."""
        idx = np.arange(len(self.node_ids)) if nodes is None else nodes
        obj = np.array(self.obj_origin) + np.array(self.obj_vel) * t_s  # (K, 3) straight lines in ECEF
        birth = np.array(self.obj_birth)
        alive = (birth <= t_s) & (t_s < birth + OBJECT_LIFE_S)
        d = obj[None, :, :] - self.node_ecef[idx][:, None, :]  # (N, K, 3)
        enu = np.einsum("nak,nik->nia", self.enu[idx], d)  # (N, K, 3): e, n, u
        horiz = np.hypot(enu[..., 0], enu[..., 1])
        el = np.degrees(np.arctan2(enu[..., 2], horiz))
        az = np.degrees(np.arctan2(enu[..., 0], enu[..., 1])) % 360.0
        rng_m = np.linalg.norm(d, axis=2)
        cam = np.einsum("nck,nik->nic", self.cam[idx], enu)  # (N, K, 3): right, down, forward
        z = cam[..., 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            fov = (z > 0) & (np.abs(cam[..., 0] / z) <= self.tan_h) & (np.abs(cam[..., 1] / z) <= self.tan_v)
        return fov & (el >= MIN_EL_DEG) & (rng_m <= MAX_RANGE_M) & alive[None, :], az, el


def heartbeat(sc: Scenario, i: int, t_ns: int, seq: int) -> dict:
    lat, lon, alt = sc.node_llh[i]
    yaw, pitch = sc.node_att[i]
    return {"type": "heartbeat", "node_id": sc.node_ids[i], "timestamp_ns": t_ns, "seq": seq,
            "gps_coordinates": {"latitude": float(lat), "longitude": float(lon), "altitude_m": float(alt)},
            "imu_orientation": {"pitch_deg": pitch, "roll_deg": 0.0, "yaw_deg": yaw},
            "camera": {"width": 1920, "height": 1080, "fps": 30.0, "fov_h_deg": 90.0, "fov_v_deg": 58.7},
            "status": {"fps": 30.0}, "software_version": "bench"}


def telemetry(sc: Scenario, i: int, k: int, t_ns: int, seq: int, az: float, el: float) -> dict:
    lat, lon, alt = sc.node_llh[i]
    yaw, pitch = sc.node_att[i]
    return {"type": "telemetry", "node_id": sc.node_ids[i],
            "gps_coordinates": {"latitude": float(lat), "longitude": float(lon), "altitude_m": float(alt)},
            "imu_orientation": {"pitch_deg": pitch, "roll_deg": 0.0, "yaw_deg": yaw}, "timestamp_ns": t_ns,
            "vector_target": {"azimuth_deg": round(az % 360.0, 6), "elevation_deg": round(el, 6)},
            "pixel_metrics": {"angular_velocity_deg_s": 1.0, "relative_intensity": 0.8}, "seq": seq,
            "track_id": f"o{k}", "anomaly_score": 0.5}


# --- sender processes --------------------------------------------------------------------------


def sender(sc: Scenario, nodes: List[int], server: Tuple[str, int], duration: float, seed: int, ready, go,
           results) -> None:
    rng = random.Random(seed)
    keys = {i: derive_node_key(MASTER_KEY.encode(), sc.node_ids[i]) for i in nodes}
    socks = {}
    for i in nodes:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        socks[i] = s
    # Geometry first (relative times), signing once the start time is known.
    frames = int(duration * HZ)
    idx = np.array(nodes)
    plan: List[Tuple[float, int, int, float, float, int]] = []  # send offset, node, object, az, el, frame
    for f in range(frames):
        mask, az, el = sc.visible(f / HZ, idx)
        for a, k in zip(*np.nonzero(mask)):
            delay = rng.uniform(0.005, 0.095)
            plan.append((f / HZ + delay, int(idx[a]), int(k), float(az[a, k]) + rng.gauss(0, NOISE_DEG),
                         float(el[a, k]) + rng.gauss(0, NOISE_DEG), f))
    plan.sort()
    seq = {i: 0 for i in nodes}

    def next_seq(i: int) -> int:
        seq[i] += 1
        return seq[i]

    for i in nodes:  # heartbeats register the nodes (and their return addresses) before the blast
        socks[i].sendto(encode_signed(heartbeat(sc, i, time.time_ns(), next_seq(i)), keys[i]), server)
    ready.put(len(plan))
    t0_ns = go.get()  # capture time of frame 0

    packets = []
    for off, i, k, az, el, f in plan:
        capture = t0_ns + f * (1_000_000_000 // HZ) + rng.randrange(-1_000_000, 1_000_001)
        packets.append((off, i, capture, encode_signed(telemetry(sc, i, k, capture, next_seq(i), az, el), keys[i])))
    received: List[Tuple[int, float, bytes]] = []
    stop = threading.Event()

    def receive() -> None:
        sel = selectors.DefaultSelector()
        for i, s in socks.items():
            s.setblocking(False)
            sel.register(s, selectors.EVENT_READ, i)
        while not stop.is_set():
            for key, _ in sel.select(0.2):
                try:
                    data = key.fileobj.recv(4096)  # type: ignore[union-attr]
                except BlockingIOError:
                    continue
                received.append((key.data, time.time(), data))

    rx = threading.Thread(target=receive, daemon=True)
    rx.start()
    start = t0_ns / 1e9
    heartbeats: List[float] = []
    sent_at: Dict[Tuple[int, int], float] = {}
    next_hb = start + 10.0
    j = 0
    while j < len(packets):
        now = time.time()
        due = start + packets[j][0]
        if due > now:
            time.sleep(min(due - now, 0.002))
            continue
        while j < len(packets) and start + packets[j][0] <= now:
            _, i, capture, data = packets[j]
            socks[i].sendto(data, server)
            sent_at[(i, capture)] = time.time()
            j += 1
        if now >= next_hb:
            next_hb += 10.0
            for i in nodes:
                socks[i].sendto(encode_signed(heartbeat(sc, i, time.time_ns(), next_seq(i)), keys[i]), server)
                heartbeats.append(time.time())
    time.sleep(2.0)
    stop.set()
    rx.join(2)
    results.put({"heartbeats": heartbeats, "sent_at": sent_at, "received": received})


# --- server, statistics, report -------------------------------------------------------------


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def cpu_seconds(pid: int) -> Optional[float]:
    """User+system CPU of a process and its children (Linux /proc)."""
    try:
        ticks = os.sysconf("SC_CLK_TCK")
        total = 0
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                fields = Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if int(entry) == pid or int(fields[1]) == pid:
                total += int(fields[11]) + int(fields[12])
        return total / ticks
    except (OSError, ValueError):
        return None


def start_redis(port: int) -> Tuple[subprocess.Popen, str]:
    workdir = tempfile.mkdtemp(prefix="skynet-bench-redis-")
    proc = subprocess.Popen(["redis-server", "--port", str(port), "--bind", "127.0.0.1", "--save", "",
                             "--appendonly", "no", "--dir", workdir], stdout=subprocess.DEVNULL)
    for _ in range(100):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc, workdir
        time.sleep(0.05)
    raise SystemExit("redis-server did not start")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=("memory", "redis"), default="memory")
    ap.add_argument("--workers", type=int, default=1, help="server worker processes (redis backend)")
    ap.add_argument("--rate", type=float, default=10_000.0, help="offered telemetry messages per second")
    ap.add_argument("--duration", type=float, default=30.0, help="measured seconds of blasting")
    ap.add_argument("--warmup", type=float, default=8.0, help="seconds of traffic before measuring")
    ap.add_argument("--senders", type=int, default=3)
    ap.add_argument("--http-port", type=int, default=8150)
    ap.add_argument("--udp-port", type=int, default=9150)
    ap.add_argument("--redis-port", type=int, default=6391, help="private redis-server started for the run")
    ap.add_argument("--batch", type=int, default=64, help="SKYNET_UDP_BATCH")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", help="also write the results to this file")
    args = ap.parse_args()

    t = time.time()
    sc = Scenario(args.rate, args.seed, args.duration)
    per_frame = round(np.mean([sc.visible(args.duration * i / 8)[0].sum() for i in range(8)]))
    print(f"scenario: {len(sc.node_ids)} nodes, {sc.objects} objects, {per_frame} reports per frame "
          f"({per_frame * HZ} msg/s offered) [{time.time() - t:.1f}s]", flush=True)

    redis_proc = redis_dir = None
    env = {k: v for k, v in os.environ.items() if not k.startswith("SKYNET_")}
    env.update(SKYNET_MASTER_KEY=MASTER_KEY, SKYNET_HTTP_HOST="127.0.0.1", SKYNET_HTTP_PORT=str(args.http_port),
               SKYNET_UDP_HOST="127.0.0.1", SKYNET_UDP_PORT=str(args.udp_port), SKYNET_UDP_BATCH=str(args.batch),
               SKYNET_STATE_BACKEND=args.backend, SKYNET_WORKERS=str(args.workers), SKYNET_LOG_LEVEL="warning",
               SKYNET_PUBLIC_URL=f"http://127.0.0.1:{args.http_port}", SKYNET_STORAGE="none")
    if args.backend == "redis":
        redis_proc, redis_dir = start_redis(args.redis_port)
        env["SKYNET_REDIS_URL"] = f"redis://127.0.0.1:{args.redis_port}/0"
    server = subprocess.Popen([sys.executable, "-m", "skynet_server", "serve"], env=env,
                              cwd=str(ROOT / "server"), start_new_session=True)
    api = f"http://127.0.0.1:{args.http_port}/api/v1"
    try:
        for _ in range(200):
            try:
                get_json(f"{api}/health")
                break
            except OSError:
                time.sleep(0.1)
        ctx = mp.get_context("fork")
        ready, results = ctx.Queue(), ctx.Queue()
        gos = [ctx.Queue() for _ in range(args.senders)]
        shares = [list(range(len(sc.node_ids)))[s::args.senders] for s in range(args.senders)]
        total = args.warmup + args.duration
        procs = [ctx.Process(target=sender, args=(sc, shares[s], ("127.0.0.1", args.udp_port), total,
                                                  args.seed * 100 + s, ready, gos[s], results))
                 for s in range(args.senders)]
        for p in procs:
            p.start()
        for _ in procs:
            ready.get()
        t0_ns = time.time_ns() + 8_000_000_000  # time to sign everything, then frame 0 is captured
        for q in gos:
            q.put(t0_ns)
        # Measure after the warm-up: a cold server starts with no partners for
        # any local track, so its first second is a burst of full searches.
        measure_from = t0_ns / 1e9 + args.warmup
        time.sleep(max(0.0, measure_from - time.time()))
        before = get_json(f"{api}/stats")
        cpu0, wall0 = cpu_seconds(server.pid), time.time()
        end = measure_from + args.duration + 0.2
        samples = []
        while time.time() < end - 1.0:
            time.sleep(min(5.0, end - time.time()))
            samples.append(get_json(f"{api}/stats"))
        time.sleep(max(0.0, end - time.time()))
        cpu1, wall = cpu_seconds(server.pid), time.time() - wall0
        time.sleep(1.5)  # let queued work and back-signals drain
        after = get_json(f"{api}/stats")
        outs = [results.get(timeout=180) for _ in procs]
        for p in procs:
            p.join(10)
        tracks = get_json(f"{api}/tracks?since_ns={t0_ns - 10**9}&limit=1000")["tracks"]
    finally:
        os.killpg(server.pid, signal.SIGTERM)
        server.wait(30)
        if redis_proc is not None:
            redis_proc.terminate()
            redis_proc.wait(10)
            shutil.rmtree(redis_dir, ignore_errors=True)

    window = (measure_from, measure_from + args.duration)
    sent = sum(sum(1 for t in o["sent_at"].values() if window[0] <= t < window[1])
               + sum(1 for t in o["heartbeats"] if window[0] <= t < window[1]) for o in outs)
    counted = (after["packets_total"] - before["packets_total"]
               + sum(after["rejected_total"].values()) - sum(before["rejected_total"].values()))
    # End-to-end back-signal: from sending the last packet of the frame among
    # the nodes that received an event's clip requests, to the first receipt.
    sent_at: Dict[Tuple[int, int], float] = {}
    for o in outs:
        sent_at.update(o["sent_at"])
    by_event: Dict[str, List[Tuple[int, float, int]]] = {}
    for o in outs:
        for node, recv_t, raw in o["received"]:
            msg = json.loads(raw)
            if msg.get("type") == "clip_request":
                match_t = msg["t_start_ns"] + 5_000_000_000
                by_event.setdefault(msg["event_id"], []).append((node, recv_t, match_t))
    e2e = []
    for recs in by_event.values():
        match_t = recs[0][2]
        sends = [sent_at[(n, match_t)] for n, _, _ in recs if (n, match_t) in sent_at]
        if sends and window[0] <= max(sends) < window[1]:
            e2e.append((min(r for _, r, _ in recs) - max(sends)) * 1000.0)
    e2e.sort()

    def pct(xs: List[float], q: float) -> Optional[float]:
        return round(xs[min(len(xs) - 1, int(q * len(xs)))], 2) if xs else None

    p99s = [s["processing_latency_ms"]["p99"] for s in samples if s["processing_latency_ms"]["p99"] is not None]
    report = {
        "backend": args.backend, "workers": args.workers, "senders": args.senders, "udp_batch": args.batch,
        "nodes": len(sc.node_ids), "objects": sc.objects, "duration_s": args.duration,
        "warmup_s": args.warmup, "sent_msgs": sent, "offered_rate": round(sent / args.duration),
        "server_counted": counted, "achieved_rate": round(counted / args.duration),
        "loss_pct": round(100.0 * max(0, sent - counted) / max(sent, 1), 3),
        "rejected": {k: v - before["rejected_total"].get(k, 0) for k, v in after["rejected_total"].items()},
        "matches": after["matches_total"] - before["matches_total"], "tracks": len(tracks),
        "processing_latency_ms_end": after["processing_latency_ms"],
        "processing_p99_ms_worst_5s_window": max(p99s) if p99s else None,
        "backsignal_latency_ms": after["backsignal_latency_ms"],
        "clip_requests_received": sum(len(r) for r in by_event.values()),
        "backsignal_e2e_ms": {"p50": pct(e2e, 0.5), "p95": pct(e2e, 0.95), "p99": pct(e2e, 0.99),
                              "max": round(e2e[-1], 2) if e2e else None, "n": len(e2e)},
        "server_cpu_cores": round((cpu1 - cpu0) / wall, 2) if cpu0 is not None and cpu1 is not None else None,
        "udp_dropped_overload": after.get("counters", {}).get("udp_dropped_overload", 0),
        "counters": {k: v - before.get("counters", {}).get(k, 0) for k, v in after.get("counters", {}).items()},
    }
    print(json.dumps(report, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
