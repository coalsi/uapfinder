"""In-process state for single-instance deployments (the fastest backend).

Everything lives in dicts and sorted lists owned by the event loop, so no
locking is needed: every method runs to completion without awaiting.
Expiring collections (replay windows, pair claims, track links) are
insertion-ordered dicts whose entries share one TTL, so the oldest entry is
always first and pruning pops from the front instead of scanning.
"""

from __future__ import annotations

import bisect
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from skynet_common.geodesy import haversine_m

from ..models import ClipRecord, Commit, Detection, NodeRecord, TrackState
from ..stats import StatsDelta
from .base import Admission, AdmitResult, StateBackend, UdpAddr, event_id_key

_CELL_DEG = 0.5
_MOVE_REINDEX_M = 50.0
_REPLAY_TTL_NS = 30_000_000_000  # > 2x the freshness window: older replays are rejected as stale
_DET_RETENTION_NS = 5_000_000_000
_PRUNE_INTERVAL_NS = 1_000_000_000

PointTuple = Tuple[int, float, float, float, float, float, Tuple[str, ...]]


class _Expiring:
    """Keys sharing one TTL, oldest first."""

    __slots__ = ("ttl_ns", "items")

    def __init__(self, ttl_ns: int):
        self.ttl_ns = ttl_ns
        self.items: Dict[str, Any] = {}

    def prune(self, now_ns: int) -> None:
        items = self.items
        while items:
            key = next(iter(items))
            if items[key][1] > now_ns:
                break
            del items[key]


class _NodeDetections:
    __slots__ = ("ts", "dets")

    def __init__(self) -> None:
        self.ts: List[int] = []
        self.dets: List[Detection] = []

    def add(self, d: Detection, cutoff_ns: int) -> None:
        ts = self.ts
        if not ts or d.t_ns >= ts[-1]:
            ts.append(d.t_ns)
            self.dets.append(d)
        else:
            i = bisect.bisect_right(ts, d.t_ns)
            ts.insert(i, d.t_ns)
            self.dets.insert(i, d)
        if ts[0] < cutoff_ns:
            cut = bisect.bisect_left(ts, cutoff_ns)
            del ts[:cut]
            del self.dets[:cut]

    def window(self, lo_ns: int, hi_ns: int) -> List[Detection]:
        i = bisect.bisect_left(self.ts, lo_ns)
        j = bisect.bisect_right(self.ts, hi_ns)
        return self.dets[i:j]


class MemoryState(StateBackend):
    name = "memory"

    def __init__(self, events_maxlen: int = 10_000, detection_retention_ns: int = _DET_RETENTION_NS,
                 max_points: int = 2_000, track_retention_ns: int = 6 * 3600 * 1_000_000_000):
        self.events_maxlen = events_maxlen
        self.detection_retention_ns = detection_retention_ns
        self.max_points = max_points
        self.track_retention_ns = track_retention_ns
        self.nodes: Dict[str, NodeRecord] = {}
        self._grid: Dict[Tuple[int, int], set] = {}
        self._indexed: Dict[str, Tuple[float, float, Tuple[int, int]]] = {}
        self._grid_version = 0
        self._neighbour_cache: Dict[str, Tuple[int, float, List[str]]] = {}
        self._detections: Dict[str, _NodeDetections] = {}
        self._replay: Dict[str, Dict[Tuple[int, int], int]] = {}
        self._claims: Dict[int, _Expiring] = {}
        self._links = _Expiring(0)
        self.tracks: Dict[str, TrackState] = {}
        self._points: Dict[str, List[PointTuple]] = {}
        self._events: List[Dict[str, Any]] = []
        self._event_keys: List[Tuple[int, int]] = []
        self._last_event = (0, -1)
        self._commands: Dict[str, List[str]] = {}
        self._clips: Dict[str, ClipRecord] = {}
        self._next_prune_ns = 0
        self._next_track_prune_ns = 0

    # --- spatial index -----------------------------------------------------

    @staticmethod
    def _cell(lat: float, lon: float) -> Tuple[int, int]:
        return int(math.floor(lat / _CELL_DEG)), int(math.floor(lon / _CELL_DEG))

    def _index(self, node_id: str, lat: float, lon: float) -> None:
        prev = self._indexed.get(node_id)
        if prev is not None and haversine_m(prev[0], prev[1], lat, lon) < _MOVE_REINDEX_M:
            return
        cell = self._cell(lat, lon)
        if prev is not None:
            self._grid[prev[2]].discard(node_id)
        self._grid.setdefault(cell, set()).add(node_id)
        self._indexed[node_id] = (lat, lon, cell)
        self._grid_version += 1

    def nodes_within(self, node_id: str, lat: float, lon: float, radius_m: float) -> List[str]:
        cached = self._neighbour_cache.get(node_id)
        if cached is not None and cached[0] == self._grid_version and cached[1] == radius_m:
            return cached[2]
        dlat = radius_m / 110_574.0
        cos_lat = math.cos(math.radians(min(89.9, abs(lat) + dlat)))
        dlon = min(180.0, radius_m / (111_320.0 * max(cos_lat, 1e-6)))
        i0, j0 = self._cell(lat - dlat, lon - dlon)
        i1, j1 = self._cell(lat + dlat, lon + dlon)
        ncols = int(round(360.0 / _CELL_DEG))
        span = min(j1 - j0, ncols - 1)
        found: List[str] = []
        for i in range(i0, i1 + 1):
            for j in range(j0, j0 + span + 1):
                # Wrap longitude cells across the antimeridian.
                jj = (j + ncols // 2) % ncols - ncols // 2
                for other in self._grid.get((i, jj), ()):
                    if other == node_id:
                        continue
                    olat, olon, _ = self._indexed[other]
                    if haversine_m(lat, lon, olat, olon) <= radius_m:
                        found.append(other)
        found.sort()
        self._neighbour_cache[node_id] = (self._grid_version, radius_m, found)
        return found

    # --- hot path ----------------------------------------------------------

    def _prune(self, now_ns: int) -> None:
        self._next_prune_ns = now_ns + _PRUNE_INTERVAL_NS
        cutoff = now_ns - _REPLAY_TTL_NS
        for node_id in list(self._replay):
            seen = self._replay[node_id]
            while seen:
                k = next(iter(seen))
                if seen[k] > cutoff:
                    break
                del seen[k]
            if not seen:
                del self._replay[node_id]
        for claims in self._claims.values():
            claims.prune(now_ns)
        self._links.prune(now_ns)
        det_cutoff = now_ns - self.detection_retention_ns
        for node_id in list(self._detections):
            nd = self._detections[node_id]
            if not nd.ts or nd.ts[-1] < det_cutoff:
                del self._detections[node_id]
        if now_ns >= self._next_track_prune_ns:
            self._next_track_prune_ns = now_ns + 60_000_000_000
            stale = now_ns - self.track_retention_ns
            for tid in [t for t, tr in self.tracks.items() if tr.last_ns < stale]:
                del self.tracks[tid]
                self._points.pop(tid, None)

    async def admit(self, items: Sequence[Admission], now_ns: int, radius_m: float,
                    udp_addrs: Optional[Dict[str, UdpAddr]] = None) -> AdmitResult:
        if now_ns >= self._next_prune_ns:
            self._prune(now_ns)
        fresh: List[bool] = []
        reporters: Dict[str, Tuple[float, float]] = {}
        refreshed: Dict[str, NodeRecord] = {}
        cutoff = now_ns - self.detection_retention_ns
        for item in items:
            u = item.update
            seen = self._replay.get(u.node_id)
            if seen is None:
                seen = self._replay[u.node_id] = {}
            key = (item.seq, u.t_ns)
            if key in seen:
                fresh.append(False)
                continue
            seen[key] = now_ns
            fresh.append(True)
            node = self.nodes.get(u.node_id)
            if node is None:
                node = self.nodes[u.node_id] = NodeRecord(u.node_id)
            node.lat, node.lon, node.alt_m = u.lat, u.lon, u.alt_m
            node.yaw_deg, node.pitch_deg, node.roll_deg = u.yaw_deg, u.pitch_deg, u.roll_deg
            if u.fov_h_deg is not None:
                node.fov_h_deg, node.fov_v_deg = u.fov_h_deg, u.fov_v_deg
                node.status = u.status or {}
                node.software_version = u.software_version or None
                refreshed[u.node_id] = node
            node.last_seen_ns = now_ns
            self._index(u.node_id, u.lat, u.lon)
            d = item.detection
            if d is not None:
                nd = self._detections.get(d.node_id)
                if nd is None:
                    nd = self._detections[d.node_id] = _NodeDetections()
                nd.add(d, cutoff)
                reporters[d.node_id] = (u.lat, u.lon)
        neighbours = {n: self.nodes_within(n, lat, lon, radius_m) for n, (lat, lon) in reporters.items()}
        return AdmitResult(fresh, neighbours, refreshed)

    async def fetch_detections(self, windows: Dict[str, Tuple[int, int]], active_since_ns: int
                               ) -> Dict[str, List[Detection]]:
        out: Dict[str, List[Detection]] = {}
        for node_id, (lo, hi) in windows.items():
            node = self.nodes.get(node_id)
            nd = self._detections.get(node_id)
            if node is None or nd is None or node.last_seen_ns < active_since_ns:
                continue
            found = nd.window(lo, hi)
            if found:
                out[node_id] = found
        return out

    async def claim(self, keys: Sequence[str], ttl_ms: int) -> List[bool]:
        claims = self._claims.get(ttl_ms)
        if claims is None:
            claims = self._claims[ttl_ms] = _Expiring(ttl_ms * 1_000_000)
        now = time.time_ns()
        expiry = now + claims.ttl_ns
        out = []
        items = claims.items
        for key in keys:
            entry = items.get(key)
            if entry is not None and entry[1] > now:
                out.append(False)
            else:
                items.pop(key, None)
                items[key] = (True, expiry)
                out.append(True)
        return out

    async def get_links(self, keys: Sequence[str]) -> Dict[str, str]:
        now = time.time_ns()
        links = self._links.items
        out = {}
        for key in keys:
            entry = links.get(key)
            if entry is not None and entry[1] > now:
                out[key] = entry[0]
        return out

    async def get_tracks(self, track_ids: Sequence[str]) -> Dict[str, TrackState]:
        return {tid: self.tracks[tid] for tid in track_ids if tid in self.tracks}

    def _next_event_id(self) -> Tuple[int, int]:
        ms = time.time_ns() // 1_000_000
        last_ms, last_seq = self._last_event
        self._last_event = (ms, 0) if ms > last_ms else (last_ms, last_seq + 1)
        return self._last_event

    async def commit(self, commit: Commit, link_ttl_ms: int, stats: Optional[StatsDelta] = None
                     ) -> List[Dict[str, Any]]:
        if commit.links:
            expiry = time.time_ns() + link_ttl_ms * 1_000_000
            links = self._links.items
            for key, tid in commit.links.items():
                links.pop(key, None)
                links[key] = (tid, expiry)
        for tid, track in commit.tracks.items():
            self.tracks[tid] = track
        for tid, points in commit.new_points.items():
            store = self._points.setdefault(tid, [])
            store.extend((p["t_ns"], p["lat"], p["lon"], p["alt_m"], p["sigma_m"], p["error_margin_m"],
                          tuple(p["node_ids"])) for p in points)
            if len(store) > self.max_points * 5 // 4:
                del store[: len(store) - self.max_points]
        for clip in commit.clips:
            self._clips[clip.request_id] = clip
        for node_id, cmds in commit.commands.items():
            self._commands.setdefault(node_id, []).extend(cmds)
        out = []
        for event in commit.events:
            key = self._next_event_id()
            stored = {"id": f"{key[0]}-{key[1]}", **event}
            self._events.append(stored)
            self._event_keys.append(key)
            out.append(stored)
        if len(self._events) > 2 * self.events_maxlen:
            excess = len(self._events) - self.events_maxlen
            del self._events[:excess]
            del self._event_keys[:excess]
        return out

    async def get_udp_addrs(self, node_ids: Sequence[str]) -> Dict[str, Tuple[UdpAddr, int]]:
        return {}  # single process: the engine's own address book is authoritative

    # --- nodes ---------------------------------------------------------------

    async def register_node(self, node_id: str, label: Optional[str], now_ns: int) -> Tuple[NodeRecord, bool]:
        node = self.nodes.get(node_id)
        created = node is None or node.registered_ns is None
        if node is None:
            node = self.nodes[node_id] = NodeRecord(node_id)
        node.label = label if label is not None else node.label
        if node.registered_ns is None:
            node.registered_ns = now_ns
        return node, created

    async def list_nodes(self) -> List[NodeRecord]:
        return sorted(self.nodes.values(), key=lambda n: n.node_id)

    async def get_node(self, node_id: str) -> Optional[NodeRecord]:
        return self.nodes.get(node_id)

    async def count_active_nodes(self, since_ns: int) -> int:
        return sum(1 for n in self.nodes.values() if n.last_seen_ns >= since_ns)

    # --- tracks --------------------------------------------------------------

    async def list_tracks(self, since_ns: int, limit: int, classification: Optional[str]) -> List[TrackState]:
        found = [t for t in self.tracks.values()
                 if t.last_ns >= since_ns and (classification is None or t.classification == classification)]
        found.sort(key=lambda t: t.last_ns, reverse=True)
        return found[:limit]

    async def get_track_points(self, track_ids: Sequence[str], limit: int) -> Dict[str, List[Dict[str, Any]]]:
        out = {}
        for tid in track_ids:
            pts = self._points.get(tid)
            if pts is not None:
                out[tid] = [
                    {"t_ns": p[0], "lat": p[1], "lon": p[2], "alt_m": p[3], "sigma_m": p[4],
                     "error_margin_m": p[5], "node_ids": list(p[6])}
                    for p in pts[-limit:]
                ]
        return out

    async def count_active_tracks(self, since_ns: int) -> int:
        return sum(1 for t in self.tracks.values() if t.last_ns >= since_ns)

    # --- events ----------------------------------------------------------------

    async def events_after(self, cursor: Optional[str], limit: int) -> List[Dict[str, Any]]:
        if cursor is None:
            return self._events[-limit:] if limit else []
        i = bisect.bisect_right(self._event_keys, event_id_key(cursor))
        return self._events[i : i + limit]

    async def last_event_id(self) -> Optional[str]:
        return self._events[-1]["id"] if self._events else None

    # --- commands and clips ------------------------------------------------------

    async def pop_commands(self, node_ids: Sequence[str]) -> Dict[str, List[str]]:
        return {n: self._commands.pop(n) for n in node_ids if n in self._commands}

    async def get_clip(self, request_id: str) -> Optional[ClipRecord]:
        return self._clips.get(request_id)

    async def put_clip(self, clip: ClipRecord) -> None:
        self._clips[clip.request_id] = clip
