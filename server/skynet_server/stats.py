"""Counters, rates and latency percentiles behind ``/stats`` and ``/metrics``.

Latencies are kept in fixed-size ring buffers of recent samples so that a
percentile query is a bounded sort and recording a sample is two list
writes on the hot path. Rates use per-second buckets averaged over the last
10 s, as API.md specifies.

With the Redis backend several processes (standalone workers or Vercel
Function instances) share one view: each process periodically hands its
deltas to the backend (``drain``), which aggregates counters, per-second
buckets and a sample of latencies in Redis.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

RATE_WINDOW_S = 10
_RING = 8192


class LatencyRing:
    """The most recent latency samples, in milliseconds."""

    __slots__ = ("_buf", "_i", "_full")

    def __init__(self, size: int = _RING):
        self._buf = [0.0] * size
        self._i = 0
        self._full = False

    def add(self, ms: float) -> None:
        self._buf[self._i] = ms
        self._i += 1
        if self._i == len(self._buf):
            self._i = 0
            self._full = True

    def samples(self) -> List[float]:
        return list(self._buf if self._full else self._buf[: self._i])


def percentiles(samples: Iterable[float]) -> Dict[str, Optional[float]]:
    data = sorted(samples)
    if not data:
        return {"p50": None, "p95": None, "p99": None, "max": None}

    def pick(q: float) -> float:
        return round(data[min(len(data) - 1, int(q * len(data)))], 4)

    return {"p50": pick(0.50), "p95": pick(0.95), "p99": pick(0.99), "max": round(data[-1], 4)}


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
    """Process-local statistics; also the accumulator for shared (Redis) stats."""

    def __init__(self) -> None:
        self.started = time.time()
        self.counters: Counter = Counter()
        self.rejected: Counter = Counter()
        self.processing = LatencyRing()
        self.backsignal = LatencyRing()
        self._buckets: Dict[int, Counter] = {}
        self._delta = StatsDelta()
        self._max_delta_samples = 256

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] += n
        self._delta.counters[name] += n
        if name in ("packets", "matches"):
            sec = int(time.time())
            bucket = self._buckets.get(sec)
            if bucket is None:
                bucket = self._buckets[sec] = Counter()
                for old in [s for s in self._buckets if s < sec - RATE_WINDOW_S - 2]:
                    del self._buckets[old]
            bucket[name] += n
            dbucket = self._delta.per_second.get(sec)
            if dbucket is None:
                dbucket = self._delta.per_second[sec] = Counter()
            dbucket[name] += n

    def reject(self, reason: str, n: int = 1) -> None:
        self.rejected[reason] += n
        self._delta.rejected[reason] += n

    def processing_ms(self, ms: float) -> None:
        self.processing.add(ms)
        if len(self._delta.processing_ms) < self._max_delta_samples:
            self._delta.processing_ms.append(ms)

    def backsignal_ms(self, ms: float) -> None:
        self.backsignal.add(ms)
        if len(self._delta.backsignal_ms) < self._max_delta_samples:
            self._delta.backsignal_ms.append(ms)

    def drain(self) -> StatsDelta:
        delta, self._delta = self._delta, StatsDelta()
        return delta

    def rate(self, name: str, now: Optional[float] = None) -> float:
        sec = int(now if now is not None else time.time())
        # Average over the last 10 complete seconds plus the current partial one,
        # weighted by the elapsed time so a fresh process does not under-report.
        elapsed = min(RATE_WINDOW_S, max(1.0, (now or time.time()) - self.started))
        total = sum(b[name] for s, b in self._buckets.items() if sec - RATE_WINDOW_S < s <= sec)
        return total / elapsed

    def snapshot(self) -> Dict[str, object]:
        now = time.time()
        return {
            "uptime_s": round(now - self.started, 3),
            "packets_total": self.counters["packets"],
            "packets_per_s": round(self.rate("packets", now), 2),
            "rejected_total": dict(self.rejected),
            "matches_total": self.counters["matches"],
            "matches_per_s": round(self.rate("matches", now), 2),
            "processing_latency_ms": percentiles(self.processing.samples()),
            "backsignal_latency_ms": percentiles(self.backsignal.samples()),
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
