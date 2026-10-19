"""Counters, rates and latency percentiles behind ``/stats`` and ``/metrics``.

Latencies are kept in fixed-size ring buffers of recent samples, so that
recording one is a few list writes on the hot path, and ``/stats`` reports
percentiles over the last 30 s. Rates use per-second buckets averaged over
the last 10 s, as API.md specifies.

With the Redis backend several processes (standalone workers or Vercel
Function instances) share one view: each process periodically hands its
deltas to the backend (``drain``), which aggregates counters, per-second
buckets and a sample of latencies in Redis.
"""

from __future__ import annotations

import random
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

RATE_WINDOW_S = 10
LATENCY_WINDOW_S = 30.0  # /stats latency percentiles cover the last 30 s
RATE_COUNTERS = ("packets", "matches")
_RING = 65536  # about 6.5 s of samples at 10k msg/s


class LatencyRing:
    """Recent latency samples (ms) with the second they were taken, so that
    percentiles can cover a recent time window rather than a sample count."""

    __slots__ = ("_buf", "_sec", "_i", "_full")

    def __init__(self, size: int = _RING):
        self._buf = [0.0] * size
        self._sec = [0] * size
        self._i = 0
        self._full = False

    def add(self, ms: float) -> None:
        i = self._i
        self._buf[i] = ms
        self._sec[i] = int(time.monotonic())
        i += 1
        if i == len(self._buf):
            i = 0
            self._full = True
        self._i = i

    def samples(self, window_s: Optional[float] = None) -> List[float]:
        n = len(self._buf) if self._full else self._i
        if window_s is None:
            return self._buf[:n]
        since = time.monotonic() - window_s
        return [v for v, s in zip(self._buf[:n], self._sec[:n]) if s >= since]


def percentiles(samples: Iterable[float]) -> Dict[str, Optional[float]]:
    """p50/p95/p99/max (NumPy: sorting 65k samples in Python would stall the
    event loop for tens of milliseconds on every /stats request)."""
    data = np.fromiter(samples, dtype=float)
    if data.size == 0:
        return {"p50": None, "p95": None, "p99": None, "max": None}
    p50, p95, p99, top = np.percentile(data, [50, 95, 99, 100], method="lower")
    return {"p50": round(float(p50), 4), "p95": round(float(p95), 4), "p99": round(float(p99), 4),
            "max": round(float(top), 4)}


@dataclass
class StatsDelta:
    """Increments accumulated since the last drain."""

    counters: Counter = field(default_factory=Counter)
    rejected: Counter = field(default_factory=Counter)
    per_second: Dict[int, Counter] = field(default_factory=dict)
    processing_ms: List[float] = field(default_factory=list)
    backsignal_ms: List[float] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.counters or self.rejected or self.per_second or self.processing_ms or self.backsignal_ms)


class Stats:
    """Process-local statistics; also the accumulator for shared (Redis) stats.

    ``shared=True`` keeps a delta of everything recorded since the last
    ``drain`` for the state backend to aggregate, at most every
    ``flush_interval_s`` (0: every batch, as a Vercel invocation must).
    Latency samples in a delta are a uniform reservoir sample. Without
    ``shared`` nothing is accumulated, so a long-running process does not grow.
    """

    _MAX_DELTA_SAMPLES = 64

    def __init__(self, shared: bool = False, flush_interval_s: float = 0.0) -> None:
        self.started = time.time()
        self.shared = shared
        self.flush_interval_s = flush_interval_s
        self.counters: Counter = Counter()
        self.rejected: Counter = Counter()
        self.processing = LatencyRing()
        self.backsignal = LatencyRing()
        self._buckets: Dict[int, Counter] = {}
        self._delta = StatsDelta()
        self._seen = [0, 0]  # samples offered to the processing / back-signal reservoirs
        self._last_drain = time.monotonic()

    def count(self, name: str, n: int = 1) -> None:
        if not n:
            return
        self.counters[name] += n
        delta = self._delta if self.shared else None
        if delta is not None:
            delta.counters[name] += n
        if name in RATE_COUNTERS:
            sec = int(time.time())
            bucket = self._buckets.get(sec)
            if bucket is None:
                bucket = self._buckets[sec] = Counter()
                for old in [s for s in self._buckets if s < sec - RATE_WINDOW_S - 2]:
                    del self._buckets[old]
            bucket[name] += n
            if delta is not None:
                dbucket = delta.per_second.get(sec)
                if dbucket is None:
                    dbucket = delta.per_second[sec] = Counter()
                dbucket[name] += n

    def reject(self, reason: str, n: int = 1) -> None:
        self.rejected[reason] += n
        if self.shared:
            self._delta.rejected[reason] += n

    def _sample(self, which: int, samples: List[float], ms: float) -> None:
        self._seen[which] += 1
        if len(samples) < self._MAX_DELTA_SAMPLES:
            samples.append(ms)
        else:
            j = random.randrange(self._seen[which])
            if j < self._MAX_DELTA_SAMPLES:
                samples[j] = ms

    def processing_ms(self, ms: float) -> None:
        self.processing.add(ms)
        if self.shared:
            self._sample(0, self._delta.processing_ms, ms)

    def backsignal_ms(self, ms: float) -> None:
        self.backsignal.add(ms)
        if self.shared:
            self._sample(1, self._delta.backsignal_ms, ms)

    def drain_due(self) -> bool:
        return self.shared and time.monotonic() - self._last_drain >= self.flush_interval_s

    def drain(self) -> StatsDelta:
        delta, self._delta = self._delta, StatsDelta()
        self._seen = [0, 0]
        self._last_drain = time.monotonic()
        return delta

    def rate(self, name: str, now: Optional[float] = None) -> float:
        """Events per second over the last ~10 s.

        Sums the current partial second and the nine before it and divides by
        the time those buckets actually cover (or the process age, if it is
        younger), so the rate is neither diluted by the partial bucket nor
        under-reported right after start.
        """
        now = time.time() if now is None else now
        sec = int(now)
        lo = sec - (RATE_WINDOW_S - 1)
        covered = min(now - lo, now - self.started)
        total = sum(b[name] for s, b in self._buckets.items() if lo <= s <= sec)
        return total / max(covered, 1.0)

    def snapshot(self) -> Dict[str, object]:
        now = time.time()
        return {
            "uptime_s": round(now - self.started, 3),
            "packets_total": self.counters["packets"],
            "packets_per_s": round(self.rate("packets", now), 2),
            "rejected_total": dict(self.rejected),
            "matches_total": self.counters["matches"],
            "matches_per_s": round(self.rate("matches", now), 2),
            "processing_latency_ms": percentiles(self.processing.samples(LATENCY_WINDOW_S)),
            "backsignal_latency_ms": percentiles(self.backsignal.samples(LATENCY_WINDOW_S)),
            "counters": dict(self.counters),
        }


def prometheus_text(snapshot: Dict[str, object]) -> str:
    """Prometheus text exposition (format 0.0.4) of a ``/stats`` snapshot."""
    lines: List[str] = []

    def metric(name: str, kind: str, help_text: str, samples: List[Tuple[str, object]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            if value is None:
                continue
            lines.append(f"{name}{labels} {value}")

    metric("skynet_uptime_seconds", "gauge", "Seconds since the statistics epoch.", [("", snapshot["uptime_s"])])
    metric("skynet_packets_total", "counter", "Valid messages accepted.", [("", snapshot["packets_total"])])
    rejected = snapshot.get("rejected_total") or {}
    assert isinstance(rejected, dict)
    metric("skynet_rejected_total", "counter", "Messages dropped, by reason.",
           [(f'{{reason="{k}"}}', v) for k, v in sorted(rejected.items())])
    metric("skynet_matches_total", "counter", "Triangulations emitted.", [("", snapshot["matches_total"])])
    metric("skynet_packets_per_second", "gauge", "Accepted messages per second (10 s average).",
           [("", snapshot["packets_per_s"])])
    metric("skynet_matches_per_second", "gauge", "Triangulations per second (10 s average).",
           [("", snapshot["matches_per_s"])])
    for key in ("active_nodes", "active_tracks"):
        if key in snapshot:
            metric(f"skynet_{key}", "gauge", f"Currently {key.replace('_', ' ')}.", [("", snapshot[key])])
    for key, name in (("processing_latency_ms", "skynet_processing_latency_ms"),
                      ("backsignal_latency_ms", "skynet_backsignal_latency_ms")):
        q = snapshot.get(key) or {}
        assert isinstance(q, dict)
        metric(name, "summary", f"{key.replace('_', ' ')} over recent samples.",
               [(f'{{quantile="{k}"}}', q.get(p)) for k, p in (("0.5", "p50"), ("0.95", "p95"), ("0.99", "p99"))])
    counters = snapshot.get("counters") or {}
    assert isinstance(counters, dict)
    metric("skynet_events_total", "counter", "Internal pipeline counters.",
           [(f'{{name="{k}"}}', v) for k, v in sorted(counters.items()) if k not in ("packets", "matches")])
    return "\n".join(lines) + "\n"
