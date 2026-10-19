#!/usr/bin/env python3
"""Single-core engine capacity, without the network or sender processes.

``bench_ingest.py`` measures the whole standalone server over UDP, but on a
small machine its sender processes compete with the server for CPU. This
benchmark feeds the same dense 200-node scenario straight into
``Engine.process`` in arrival order (64-message batches, like the UDP
listener) and reports how many messages per second one process can match.
Run it on the machine that will host the server, e.g. the Mac Studio:

    python server/bench/bench_inprocess.py            # 12 s of scenario
    python server/bench/bench_inprocess.py --profile  # plus a cProfile table

Packets are generated and signed before the clock starts, so the figure is
decode + verify + match + track + back-signal routing, with the state in
memory and no database.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import pstats
import random
import sys
import time
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "server" / "bench"), str(ROOT / "server"), str(ROOT / "common")]

import numpy as np  # noqa: E402

import bench_ingest as scenario  # noqa: E402
from skynet_common.protocol import derive_node_key, encode_signed  # noqa: E402
from skynet_server.config import Settings  # noqa: E402
from skynet_server.engine import Engine, Inbound  # noqa: E402
from skynet_server.live import EventHub  # noqa: E402
from skynet_server.state import create_state  # noqa: E402
from skynet_server.stats import Stats  # noqa: E402

BATCH = 64


class _DiscardSender:
    """Stands in for the UDP socket: back-signals are routed but not sent."""

    def sendto(self, data: bytes, addr: Tuple[str, int]) -> None:
        pass


def build_messages(seconds: float, rate: float, seed: int) -> Tuple[List[Tuple[int, bytes, int]], int, int]:
    """Heartbeats then telemetry, sorted by simulated arrival time."""
    sc = scenario.Scenario(rate, seed, (2.0, seconds))
    rng = random.Random(seed)
    keys = [derive_node_key(scenario.MASTER_KEY.encode(), n) for n in sc.node_ids]
    base = time.time_ns()
    seq = [0] * len(sc.node_ids)
    out: List[Tuple[int, bytes, int]] = []
    for i in range(len(sc.node_ids)):
        seq[i] += 1
        out.append((base - 1, encode_signed(scenario.heartbeat(sc, i, base, seq[i]), keys[i]), i))
    for f in range(int(seconds * scenario.HZ)):
        mask, az, el = sc.visible(f / scenario.HZ)
        for a, k in zip(*np.nonzero(mask)):
            capture = base + f * (1_000_000_000 // scenario.HZ) + rng.randrange(-1_000_000, 1_000_001)
            arrival = capture + int(rng.uniform(0.005, 0.095) * 1e9)
            seq[a] += 1
            msg = scenario.telemetry(sc, int(a), int(k), capture, seq[a],
                                     float(az[a, k]) + rng.gauss(0, scenario.NOISE_DEG),
                                     float(el[a, k]) + rng.gauss(0, scenario.NOISE_DEG))
            out.append((arrival, encode_signed(msg, keys[a]), int(a)))
    out.sort(key=lambda m: m[0])
    return out, len(sc.node_ids), sc.objects


async def feed(engine: Engine, messages: List[Tuple[int, bytes, int]]) -> Tuple[float, int]:
    start = time.perf_counter()
    matches = 0
    for j in range(0, len(messages), BATCH):
        now_perf = time.perf_counter_ns()
        batch = [Inbound(data, arrival, now_perf, ("127.0.0.1", 40000 + node)) for arrival, data, node in
                 messages[j : j + BATCH]]
        matches += (await engine.process(batch)).matches
    return time.perf_counter() - start, matches


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=12.0, help="scenario seconds to generate")
    ap.add_argument("--rate", type=float, default=10_000.0, help="telemetry messages per second in the scenario")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--profile", action="store_true", help="print the 25 most expensive functions")
    args = ap.parse_args()

    messages, nodes, objects = build_messages(args.seconds, args.rate, args.seed)
    telemetry = len(messages) - nodes
    print(f"scenario: {nodes} nodes, {objects} objects, {telemetry} telemetry messages "
          f"({telemetry / args.seconds:.0f} msg/s of scenario time)")

    settings = Settings.from_env({
        "SKYNET_MASTER_KEY": scenario.MASTER_KEY, "SKYNET_ADMIN_TOKEN": "bench-admin-token-not-secret",
        "SKYNET_UDP_PORT": "0", "SKYNET_STORAGE": "none", "SKYNET_PUBLIC_URL": "http://bench.invalid:8000",
    })
    engine = Engine(settings, create_state(settings), Stats(), EventHub())
    engine.transport = _DiscardSender()

    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    elapsed, matches = asyncio.run(feed(engine, messages))
    if profiler:
        profiler.disable()

    rate = len(messages) / elapsed
    print(f"processed {len(messages)} messages in {elapsed:.2f} s: {rate:,.0f} msg/s on one core "
          f"({elapsed / len(messages) * 1e6:.0f} us/msg), {matches} matches")
    mm = engine.matchmaker
    print(f"searches: {mm.full_searches} full, {mm.guided_searches} guided; "
          f"{mm.refinements_skipped} redundant refinements skipped")
    verdict = "meets" if rate >= 10_000 else "is below"
    print(f"single-process capacity {verdict} the blueprint's 10,000 msg/s"
          + (" (profiling slows it down; rerun without --profile)" if profiler else ""))
    if profiler:
        pstats.Stats(profiler).sort_stats("tottime").print_stats(25)
    return 0


if __name__ == "__main__":
    sys.exit(main())
