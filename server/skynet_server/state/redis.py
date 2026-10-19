"""Redis state backend: shared state for several instances and for Vercel.

Vercel Functions keep nothing between invocations and several standalone
instances (one per region, say) may serve the same network, so everything
the pipeline needs lives in Redis under one key prefix:

    n:<node>      HASH  node record (position, attitude, camera, status, label)
    nodes         SET   every node id ever seen or registered
    seen          ZSET  node -> last seen (us)        "active" = seen < 60 s ago
    geo           GEO   node positions                GEOSEARCH ... BYRADIUS
    d:<node>      ZSET  packed detections, score = timestamp in microseconds
    r:<node>      ZSET  "<seq>:<timestamp_ns>" -> first seen (us), replay window
    p:<pair>      STR   pair claim (SET NX PX)
    l:<node|trk>  STR   local track -> 3D track link (PX link TTL)
    t:<track>     STR   live track state; ti ZSET track -> last point (us)
    tp:<track>    LIST  track points (capped); tc:<track> SET clip request ids
    c:<request>   STR   clip record
    q:<node>      LIST  pending signed commands (LPOP count = once-only)
    ev            STREAM live events (XADD MAXLEN ~), ids are the API cursors
    udp           HASH  node -> "host|port|seen_ns" UDP return addresses
    st:*          aggregated statistics (Vercel instances); rl:<key> rate limits

Detection scores are microseconds, not nanoseconds: Redis sorted-set scores
are doubles, exact only up to 2^53 (~9.0e15), and a current timestamp is
~1.8e18 ns but ~1.8e15 us. The member carries the exact nanoseconds.

Round trips: neighbour lists are cached per process for a few seconds (nodes
rarely move), so a batch normally costs one pipeline for admission plus the
neighbour detection fetch (store first, then query, in one pipeline), and
only batches with matches pay for claims, links, tracks and the commit.
Detections are immutable, so each process caches the ones it has stored or
fetched and only asks Redis for what is new (``_DetectionCache``).
Node-record writes are skipped when nothing changed and last-seen updates
are throttled to one per second per node and process.
"""

from __future__ import annotations

import hashlib
import secrets
import struct
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import orjson

from skynet_common.geodesy import haversine_m

from ..matchmaker import DetectionSource, NodeSamples, TrackSamples, Window
from ..models import ClipRecord, Commit, Detection, NodeRecord, TrackState
from ..stats import RATE_WINDOW_S, StatsDelta, percentiles
from .base import Admission, AdmitResult, StateBackend, UdpAddr

_DET = struct.Struct("<qq5d3d3d")  # t_ns, seq, lat, lon, alt, az, el, origin xyz, direction xyz
_NEIGHBOUR_TTL_S = 5.0
_SEEN_EVERY_NS = 1_000_000_000
_PRUNE_EVERY_NS = 1_000_000_000
_MOVE_REINDEX_M = 50.0
_LATE_ARRIVAL_NS = 100_000_000
_GEO_MAX_LAT = 85.05112878  # GEOADD's latitude limit (Web Mercator)
_CLIP_TTL_MS = 30 * 24 * 3600 * 1000
_COMMAND_TTL_MS = 3600 * 1000
_MAX_COMMANDS_PER_NODE = 1000
_LATENCY_SAMPLES = 4096
_NODE_FLOATS = ("lat", "lon", "alt_m", "yaw_deg", "pitch_deg", "roll_deg", "fov_h_deg", "fov_v_deg")


_CLAIM_LUA = """
local out = {}
for i, key in ipairs(KEYS) do
  out[i] = redis.call('SET', key, '1', 'NX', 'PX', ARGV[1]) and 1 or 0
end
return out
"""


# KEYS: replay and detection key of each item; ARGV: now (us), then per item
# the replay member, detection score and packed detection ("" for heartbeats).
# Returns 1 per fresh item and 0 per replay; replays store nothing.
_ADMIT_LUA = """
local out = {}
for i = 1, #KEYS / 2 do
  local fresh = redis.call('ZADD', KEYS[2 * i - 1], 'NX', ARGV[1], ARGV[3 * i - 1])
  out[i] = fresh
  if fresh == 1 and ARGV[3 * i + 1] ~= '' then
    redis.call('ZADD', KEYS[2 * i], ARGV[3 * i], ARGV[3 * i + 1])
  end
end
return out
"""


_SCRIPTS = {"claim": _CLAIM_LUA, "admit": _ADMIT_LUA}
# EVALSHA names a script by the SHA-1 of its source.
_SHAS = {name: hashlib.sha1(source.encode()).hexdigest() for name, source in _SCRIPTS.items()}


def pack_detection(d: Detection) -> bytes:
    head = _DET.pack(d.t_ns, d.seq, d.lat, d.lon, d.alt_m, d.az_deg, d.el_deg, *d.origin, *d.direction)
    return head + d.track_id.encode() if d.track_id is not None else head


def unpack_detection(node_id: str, raw: bytes) -> Detection:
    v = _DET.unpack_from(raw)
    track = raw[_DET.size:].decode() if len(raw) > _DET.size else None
    return Detection(node_id, v[0], v[1], track, v[2], v[3], v[4], v[5], v[6], (v[7], v[8], v[9]),
                     (v[10], v[11], v[12]))


def _us(ns: int) -> int:
    return ns // 1000


def _node_from_hash(node_id: str, h: Dict[bytes, bytes], seen_us: Optional[float]) -> NodeRecord:
    node = NodeRecord(node_id)
    for name in _NODE_FLOATS:
        raw = h.get(name.encode())
        if raw is not None:
            setattr(node, name, float(raw))
    if b"label" in h:
        node.label = h[b"label"].decode()
    if b"registered_ns" in h:
        node.registered_ns = int(h[b"registered_ns"])
    if b"status" in h:
        node.status = orjson.loads(h[b"status"])
    if h.get(b"software_version"):
        node.software_version = h[b"software_version"].decode()
    if seen_us is not None:
        node.last_seen_ns = int(seen_us) * 1000
    return node


class _DetectionCache:
    """Detections this process has stored or fetched, as a DetectionSource.

    Detections never change once stored, so each one is fetched from Redis
    once per process: ``coverage[node] = (lo, hi)`` records that every
    detection of that node with lo <= t_ns <= hi is cached. ``hi`` trails the
    wall clock by ``_LATE_ARRIVAL_NS``, so detections another instance stores
    a little late are still fetched. One that arrives later still is missed
    by this process only: the instance that stored it queried after storing
    and so matched it against this process's detections.
    """

    def __init__(self, retention_ns: int):
        self.retention_ns = retention_ns
        self.nodes: Dict[str, NodeSamples] = {}
        self.coverage: Dict[str, Tuple[int, int]] = {}

    def add(self, d: Detection) -> None:
        ns = self.nodes.get(d.node_id)
        if ns is None:
            ns = self.nodes[d.node_id] = NodeSamples()
        ns.add(d, d.t_ns - self.retention_ns)

    def needed(self, windows: Dict[str, Window]) -> Dict[str, Window]:
        """The parts of ``windows`` that must still be fetched."""
        out = {}
        for node_id, (lo, hi) in windows.items():
            cov = self.coverage.get(node_id)
            if cov is not None and cov[0] <= lo <= cov[1]:
                if hi > cov[1]:
                    out[node_id] = (cov[1], hi)
            else:
                out[node_id] = (lo, hi)
        return out

    def fetched(self, node_id: str, window: Window, now_ns: int) -> None:
        lo, hi = window
        complete = min(hi, now_ns - _LATE_ARRIVAL_NS)
        cov = self.coverage.get(node_id)
        if cov is not None and cov[0] <= lo <= cov[1]:
            self.coverage[node_id] = (cov[0], max(cov[1], complete))
        elif complete >= lo:
            self.coverage[node_id] = (lo, complete)

    def prune(self, now_ns: int) -> None:
        cutoff = now_ns - 2 * self.retention_ns
        for node_id in [n for n, ns in self.nodes.items() if ns.newest_ns < cutoff]:
            del self.nodes[node_id]
            self.coverage.pop(node_id, None)
        for node_id, ns in self.nodes.items():
            floor = ns.newest_ns - self.retention_ns
            ns.prune(floor)
            cov = self.coverage.get(node_id)
            if cov is not None and cov[0] < floor:
                self.coverage[node_id] = (floor, cov[1]) if cov[1] >= floor else (floor, floor)

    def local_track(self, node_id: str, track_id: str) -> Optional[TrackSamples]:
        ns = self.nodes.get(node_id)
        return ns.tracks.get(track_id) if ns is not None else None

    def local_tracks(self, node_id: str) -> Iterable[TrackSamples]:
        ns = self.nodes.get(node_id)
        return ns.tracks.values() if ns is not None else ()

    def untracked(self, node_id: str, lo_ns: int, hi_ns: int) -> Sequence[Detection]:
        ns = self.nodes.get(node_id)
        return ns.untracked(lo_ns, hi_ns) if ns is not None else ()


class RedisState(StateBackend):
    name = "redis"
    prefetch = True

    def __init__(self, url: str, prefix: str = "skynet:", events_maxlen: int = 10_000,
                 detection_retention_ns: int = 5_000_000_000, max_points: int = 2_000,
                 track_retention_ns: int = 6 * 3600 * 1_000_000_000, replay_window_ns: int = 30_000_000_000,
                 node_active_ns: int = 60_000_000_000):
        self.url = url
        self.p = prefix
        self.events_maxlen = events_maxlen
        self.detection_retention_ns = detection_retention_ns
        self.max_points = max_points
        self.track_retention_ms = track_retention_ns // 1_000_000
        self.replay_window_ns = replay_window_ns
        self.node_active_ns = node_active_ns
        self.r: Any = None
        # Per-process caches; purely optimisations (every value is re-derivable).
        # node -> (expiry (monotonic s), radius, (lat, lon) when cached, neighbour ids)
        self._neighbours: Dict[str, Tuple[float, float, Tuple[float, float], List[str]]] = {}
        self._written: Dict[str, Tuple[float, ...]] = {}
        self._geo: Dict[str, Tuple[float, float]] = {}
        self._seen: Dict[str, int] = {}
        self._pruned: Dict[str, int] = {}
        self._newest: Dict[str, int] = {}
        self._known: set = set()
        self._next_track_prune = 0.0
        self._cache = _DetectionCache(detection_retention_ns)
        self._links_written: Dict[str, Tuple[str, float]] = {}
        self._next_cache_prune = 0

    def k(self, *parts: str) -> str:
        return self.p + ":".join(parts)

    async def start(self) -> None:
        import redis.asyncio as aioredis

        # rediss:// URLs (Upstash) negotiate TLS; health checks recover from
        # connections that a serverless freeze or a NAT silently dropped.
        self.r = aioredis.Redis.from_url(self.url, decode_responses=False, socket_keepalive=True,
                                         health_check_interval=30, socket_connect_timeout=5, socket_timeout=10)

    async def _execute(self, pipe: Any) -> List[Any]:
        """Execute a pipeline that may contain EVALSHA calls. If Redis does
        not know a script (first use, or after a restart) the scripts are
        loaded and the commands sent again; every queued command is
        idempotent (NX writes, reads, same-value updates), so that is safe."""
        from redis.exceptions import NoScriptError

        stack = list(pipe.command_stack)
        try:
            return await pipe.execute()
        except NoScriptError:
            for source in _SCRIPTS.values():
                await self.r.script_load(source)
            again = self.r.pipeline(transaction=False)
            for args, options in stack:
                again.execute_command(*args, **options)
            return await again.execute()

    async def close(self) -> None:
        if self.r is not None:
            await self.r.aclose()
            self.r = None

    async def ping(self) -> bool:
        try:
            return bool(await self.r.ping())
        except Exception:  # noqa: BLE001 - health reporting must not raise
            return False

    # --- ingest hot path -------------------------------------------------

    async def neighbours(self, reporters: Dict[str, Tuple[float, float]], radius_m: float, now_ns: int
                         ) -> Dict[str, List[str]]:
        mono = time.monotonic()
        out: Dict[str, List[str]] = {}
        misses: List[Tuple[str, float, float]] = []
        for node_id, (lat, lon) in reporters.items():
            c = self._neighbours.get(node_id)
            if (c is not None and c[0] > mono and c[1] == radius_m
                    and haversine_m(c[2][0], c[2][1], lat, lon) < _MOVE_REINDEX_M):
                out[node_id] = c[3]
            else:
                misses.append((node_id, lat, lon))
        if not misses:
            return out
        pipe = self.r.pipeline(transaction=False)
        for _, lat, lon in misses:
            pipe.geosearch(self.k("geo"), longitude=lon, latitude=max(-_GEO_MAX_LAT, min(_GEO_MAX_LAT, lat)),
                           radius=radius_m, unit="m")
        found = await pipe.execute()
        members = sorted({m for res in found for m in res})
        active: set = set()
        if members:
            scores = await self.r.zmscore(self.k("seen"), members)
            since = _us(now_ns - self.node_active_ns)
            active = {m.decode() for m, s in zip(members, scores) if s is not None and s >= since}
        for (node_id, lat, lon), res in zip(misses, found):
            ids = sorted(m.decode() for m in res if m.decode() in active and m.decode() != node_id)
            self._neighbours[node_id] = (mono + _NEIGHBOUR_TTL_S, radius_m, (lat, lon), ids)
            out[node_id] = ids
        return out

    async def admit(self, items: Sequence[Admission], now_ns: int, windows: Optional[Dict[str, Window]] = None
                    ) -> AdmitResult:
        p = self.k
        now_us = _us(now_ns)
        pipe = self.r.pipeline(transaction=False)
        # Replay checks and detection writes for the whole batch in one script call.
        keys: List[str] = []
        args: List[Any] = [now_us]
        for item in items:
            u, d = item.update, item.detection
            keys += (p("r", u.node_id), p("d", u.node_id))
            if d is None:
                args += (f"{item.seq}:{u.t_ns}", 0, b"")
            else:
                args += (f"{item.seq}:{u.t_ns}", _us(d.t_ns), pack_detection(d))
        pipe.evalsha(_SHAS["admit"], len(keys), *keys, *args)
        n = 1
        heartbeat_at: Dict[str, int] = {}
        for item in items:
            u = item.update
            node_id = u.node_id
            if node_id not in self._known:
                pipe.sadd(p("nodes"), node_id)
                n += 1
                self._known.add(node_id)
            fields: Dict[str, Any] = {}
            pose = (u.lat, u.lon, u.alt_m, u.yaw_deg, u.pitch_deg, u.roll_deg)
            if self._written.get(node_id) != pose:
                self._written[node_id] = pose
                fields.update(lat=u.lat, lon=u.lon, alt_m=u.alt_m, yaw_deg=u.yaw_deg, pitch_deg=u.pitch_deg,
                              roll_deg=u.roll_deg)
            if u.is_heartbeat:
                fields.update(fov_h_deg=u.fov_h_deg, fov_v_deg=u.fov_v_deg, status=orjson.dumps(u.status or {}),
                              software_version=u.software_version or "")
            if fields:
                pipe.hset(p("n", node_id), mapping=fields)
                n += 1
            if u.is_heartbeat or now_ns - self._seen.get(node_id, 0) >= _SEEN_EVERY_NS:
                self._seen[node_id] = now_ns
                pipe.zadd(p("seen"), {node_id: now_us})
                n += 1
            geo = self._geo.get(node_id)
            if geo is None or haversine_m(geo[0], geo[1], u.lat, u.lon) >= _MOVE_REINDEX_M:
                self._geo[node_id] = (u.lat, u.lon)
                pipe.geoadd(p("geo"), (u.lon, max(-_GEO_MAX_LAT, min(_GEO_MAX_LAT, u.lat)), node_id))
                n += 1
            if u.is_heartbeat:
                pipe.hgetall(p("n", node_id))
                heartbeat_at[node_id] = n
                n += 1
            d = item.detection
            if d is not None:
                self._cache.add(d)  # a replay is the same detection, so this is idempotent
                if d.t_ns > self._newest.get(node_id, 0):
                    self._newest[node_id] = d.t_ns
            if now_ns - self._pruned.get(node_id, 0) >= _PRUNE_EVERY_NS:
                self._pruned[node_id] = now_ns
                newest = self._newest.get(node_id, now_ns)
                pipe.zremrangebyscore(p("d", node_id), "-inf", f"({_us(newest - self.detection_retention_ns)}")
                pipe.pexpire(p("d", node_id), (2 * self.detection_retention_ns + self.replay_window_ns) // 1_000_000)
                pipe.zremrangebyscore(p("r", node_id), "-inf", f"({_us(now_ns - self.replay_window_ns)}")
                pipe.pexpire(p("r", node_id), self.replay_window_ns // 1_000_000)
                n += 4
        if now_ns >= self._next_cache_prune:
            self._next_cache_prune = now_ns + _PRUNE_EVERY_NS
            self._cache.prune(now_ns)
        # Queued after this batch's own writes: store first, then query.
        fetch_order = list(self._cache.needed(windows).items()) if windows else []
        for node_id, (lo, hi) in fetch_order:
            pipe.zrangebyscore(p("d", node_id), _us(lo), _us(hi) + 1)
        res = await self._execute(pipe)
        fresh = [r == 1 for r in res[0]]
        refreshed: Dict[str, NodeRecord] = {}
        for node_id, i in heartbeat_at.items():
            node = _node_from_hash(node_id, res[i], now_us)
            refreshed[node_id] = node
        self._absorb(fetch_order, res[len(res) - len(fetch_order):], now_ns)
        return AdmitResult(fresh, self._cache, refreshed)

    def _absorb(self, order: List[Tuple[str, Window]], results: List[List[bytes]], now_ns: int) -> None:
        cache = self._cache
        for (node_id, window), members in zip(order, results):
            for m in members:
                cache.add(unpack_detection(node_id, m))
            cache.fetched(node_id, window, now_ns)

    async def fetch_more(self, source: DetectionSource, windows: Dict[str, Window]) -> None:
        need = list(self._cache.needed(windows).items())
        if not need:
            return
        pipe = self.r.pipeline(transaction=False)
        for node_id, (lo, hi) in need:
            pipe.zrangebyscore(self.k("d", node_id), _us(lo), _us(hi) + 1)
        self._absorb(need, await pipe.execute(), time.time_ns())

    async def claim(self, keys: Sequence[str], ttl_ms: int) -> List[bool]:
        if not keys:
            return []
        # One script call instead of one SET NX per key: the per-command
        # client overhead dominates at thousands of claims per second.
        pipe = self.r.pipeline(transaction=False)
        pipe.evalsha(_SHAS["claim"], len(keys), *[self.k("p", key) for key in keys], ttl_ms)
        return [bool(r) for r in (await self._execute(pipe))[0]]

    async def get_links(self, keys: Sequence[str]) -> Dict[str, str]:
        if not keys:
            return {}
        values = await self.r.mget([self.k("l", key) for key in keys])
        return {key: v.decode() for key, v in zip(keys, values) if v is not None}

    async def get_tracks(self, track_ids: Sequence[str]) -> Dict[str, TrackState]:
        if not track_ids:
            return {}
        values = await self.r.mget([self.k("t", tid) for tid in track_ids])
        return {tid: TrackState.from_row(orjson.loads(v)) for tid, v in zip(track_ids, values) if v is not None}

    async def commit(self, commit: Commit, link_ttl_ms: int, stats: Optional[StatsDelta] = None
                     ) -> List[Dict[str, Any]]:
        p = self.k
        pipe = self.r.pipeline(transaction=False)
        n = 0
        mono = time.monotonic()
        refresh_s = link_ttl_ms / 4000.0
        for key, tid in commit.links.items():
            # Links are rewritten by nearly every match; skip writes that would
            # neither change the value nor matter for its expiry yet.
            prev = self._links_written.get(key)
            if prev is None or prev[0] != tid or mono - prev[1] >= refresh_s:
                self._links_written[key] = (tid, mono)
                pipe.set(p("l", key), tid, px=link_ttl_ms)
                n += 1
        if len(self._links_written) > 200_000:
            self._links_written = {k: v for k, v in self._links_written.items() if mono - v[1] < 4 * refresh_s}
        retention = self.track_retention_ms
        for tid, track in commit.tracks.items():
            pipe.set(p("t", tid), orjson.dumps(track.to_row()), px=retention)
            pipe.zadd(p("ti"), {tid: _us(track.last_ns)})
            n += 2
        for tid, points in commit.new_points.items():
            pipe.rpush(p("tp", tid), *[orjson.dumps(pt) for pt in points])
            pipe.ltrim(p("tp", tid), -self.max_points, -1)
            pipe.pexpire(p("tp", tid), retention)
            n += 3
        for clip in commit.clips:
            self._put_clip(pipe, clip)
            n += 3
        for node_id, cmds in commit.commands.items():
            pipe.rpush(p("q", node_id), *cmds)
            pipe.ltrim(p("q", node_id), -_MAX_COMMANDS_PER_NODE, -1)
            pipe.pexpire(p("q", node_id), _COMMAND_TTL_MS)
            n += 3
        if commit.udp_addrs:
            pipe.hset(p("udp"), mapping={node: f"{a[0]}|{a[1]}|{seen}" for node, (a, seen) in commit.udp_addrs.items()})
            n += 1
        if stats is not None and not stats.is_empty():
            n += self._stats_commands(pipe, stats)
        if mono >= self._next_track_prune:
            self._next_track_prune = mono + 60.0
            pipe.zremrangebyscore(p("ti"), "-inf", f"({_us(time.time_ns()) - retention * 1000}")
            n += 1
        first_event = n
        for event in commit.events:
            pipe.xadd(p("ev"), {"e": orjson.dumps(event)}, maxlen=self.events_maxlen, approximate=True)
        res = await pipe.execute()
        return [{"id": eid.decode(), **event} for eid, event in zip(res[first_event:], commit.events)]

    def _stats_commands(self, pipe: Any, delta: StatsDelta) -> int:
        p = self.k
        n = 0
        for name, value in delta.counters.items():
            pipe.hincrby(p("st", "c"), name, value)
            n += 1
        for reason, value in delta.rejected.items():
            pipe.hincrby(p("st", "r"), reason, value)
            n += 1
        for sec, counter in delta.per_second.items():
            for name, value in counter.items():
                pipe.hincrby(p("st", "s", str(sec)), name, value)
                n += 1
            pipe.expire(p("st", "s", str(sec)), 3 * RATE_WINDOW_S)
            n += 1
        for key, samples in (("lp", delta.processing_ms), ("lb", delta.backsignal_ms)):
            if samples:
                pipe.lpush(p("st", key), *[f"{s:.4f}" for s in samples])
                pipe.ltrim(p("st", key), 0, _LATENCY_SAMPLES - 1)
                n += 2
        return n

    async def get_udp_addrs(self, node_ids: Sequence[str]) -> Dict[str, Tuple[UdpAddr, int]]:
        if not node_ids:
            return {}
        out = {}
        for node_id, raw in zip(node_ids, await self.r.hmget(self.k("udp"), list(node_ids))):
            if raw is not None:
                host, port, seen = raw.decode().rsplit("|", 2)
                out[node_id] = ((host, int(port)), int(seen))
        return out

    # --- nodes -----------------------------------------------------------

    async def register_node(self, node_id: str, label: Optional[str], now_ns: int) -> Tuple[NodeRecord, bool]:
        key = self.k("n", node_id)
        pipe = self.r.pipeline(transaction=False)
        pipe.hsetnx(key, "registered_ns", now_ns)
        if label is not None:
            pipe.hset(key, "label", label)
        pipe.sadd(self.k("nodes"), node_id)
        pipe.hgetall(key)
        pipe.zscore(self.k("seen"), node_id)
        res = await pipe.execute()
        return _node_from_hash(node_id, res[-2], res[-1]), bool(res[0])

    async def list_nodes(self) -> List[NodeRecord]:
        ids = sorted(m.decode() for m in await self.r.smembers(self.k("nodes")))
        if not ids:
            return []
        pipe = self.r.pipeline(transaction=False)
        for node_id in ids:
            pipe.hgetall(self.k("n", node_id))
        pipe.zmscore(self.k("seen"), ids)
        res = await pipe.execute()
        return [_node_from_hash(node_id, h, s) for node_id, h, s in zip(ids, res[:-1], res[-1])]

    async def get_node(self, node_id: str) -> Optional[NodeRecord]:
        pipe = self.r.pipeline(transaction=False)
        pipe.hgetall(self.k("n", node_id))
        pipe.zscore(self.k("seen"), node_id)
        h, seen = await pipe.execute()
        return _node_from_hash(node_id, h, seen) if h or seen is not None else None

    async def count_active_nodes(self, since_ns: int) -> int:
        return int(await self.r.zcount(self.k("seen"), _us(since_ns), "+inf"))

    # --- tracks ----------------------------------------------------------

    async def list_tracks(self, since_ns: int, limit: int, classification: Optional[str]) -> List[TrackState]:
        # A classification filter may discard most candidates, so look further.
        scan = limit if classification is None else max(limit, 5000)
        ids = [m.decode() for m in await self.r.zrevrangebyscore(self.k("ti"), "+inf", _us(since_ns), 0, scan)]
        tracks = list((await self.get_tracks(ids)).values())
        if classification is not None:
            tracks = [t for t in tracks if t.classification == classification]
        tracks.sort(key=lambda t: t.last_ns, reverse=True)
        return [t for t in tracks if t.last_ns >= since_ns][:limit]

    async def get_track_points(self, track_ids: Sequence[str], limit: int) -> Dict[str, List[Dict[str, Any]]]:
        if not track_ids:
            return {}
        pipe = self.r.pipeline(transaction=False)
        for tid in track_ids:
            pipe.lrange(self.k("tp", tid), -limit, -1)
        return {tid: [orjson.loads(x) for x in pts] for tid, pts in zip(track_ids, await pipe.execute()) if pts}

    async def count_active_tracks(self, since_ns: int) -> int:
        return int(await self.r.zcount(self.k("ti"), _us(since_ns), "+inf"))

    # --- events ----------------------------------------------------------

    @staticmethod
    def _events(entries: Iterable[Tuple[bytes, Dict[bytes, bytes]]]) -> List[Dict[str, Any]]:
        return [{"id": eid.decode(), **orjson.loads(fields[b"e"])} for eid, fields in entries]

    async def events_after(self, cursor: Optional[str], limit: int) -> List[Dict[str, Any]]:
        if limit <= 0:
            return []
        if cursor is None:
            entries = await self.r.xrevrange(self.k("ev"), "+", "-", count=limit)
            return self._events(reversed(entries))
        # "(" makes the start exclusive (Redis >= 6.2).
        return self._events(await self.r.xrange(self.k("ev"), f"({cursor}", "+", count=limit))

    async def last_event_id(self) -> Optional[str]:
        entries = await self.r.xrevrange(self.k("ev"), "+", "-", count=1)
        return entries[0][0].decode() if entries else None

    # --- commands and clips ----------------------------------------------

    async def pop_commands(self, node_ids: Sequence[str]) -> Dict[str, List[str]]:
        if not node_ids:
            return {}
        pipe = self.r.pipeline(transaction=False)
        for node_id in node_ids:
            # LPOP with a count is atomic, so concurrent pollers never get the
            # same command twice.
            pipe.lpop(self.k("q", node_id), _MAX_COMMANDS_PER_NODE)
        return {node_id: [c.decode() for c in cmds]
                for node_id, cmds in zip(node_ids, await pipe.execute()) if cmds}

    def _put_clip(self, pipe: Any, clip: ClipRecord) -> None:
        pipe.set(self.k("c", clip.request_id), orjson.dumps(clip.to_dict()), px=_CLIP_TTL_MS)
        pipe.sadd(self.k("tc", clip.track_id), clip.request_id)
        pipe.pexpire(self.k("tc", clip.track_id), _CLIP_TTL_MS)

    async def get_clip(self, request_id: str) -> Optional[ClipRecord]:
        raw = await self.r.get(self.k("c", request_id))
        return ClipRecord.from_dict(orjson.loads(raw)) if raw else None

    async def put_clip(self, clip: ClipRecord) -> None:
        pipe = self.r.pipeline(transaction=False)
        self._put_clip(pipe, clip)
        await pipe.execute()

    async def track_clips(self, track_id: str) -> List[ClipRecord]:
        ids = sorted(m.decode() for m in await self.r.smembers(self.k("tc", track_id)))
        if not ids:
            return []
        values = await self.r.mget([self.k("c", rid) for rid in ids])
        clips = [ClipRecord.from_dict(orjson.loads(v)) for v in values if v]
        return sorted(clips, key=lambda c: c.issued_ns)

    # --- misc --------------------------------------------------------------

    async def rate_limit(self, key: str, limit: int, window_s: float) -> bool:
        now = time.time()
        k = self.k("rl", key)
        pipe = self.r.pipeline(transaction=True)
        pipe.zremrangebyscore(k, "-inf", now - window_s)
        pipe.zadd(k, {f"{now:.6f}-{secrets.token_hex(4)}": now})
        pipe.zcard(k)
        pipe.pexpire(k, int(window_s * 1000) + 1000)
        res = await pipe.execute()
        return int(res[2]) <= limit

    async def shared_stats(self) -> Optional[Dict[str, Any]]:
        p = self.k
        now = time.time()
        sec = int(now)
        secs = list(range(sec - (RATE_WINDOW_S - 1), sec + 1))
        pipe = self.r.pipeline(transaction=False)
        pipe.hgetall(p("st", "c"))
        pipe.hgetall(p("st", "r"))
        for s in secs:
            pipe.hgetall(p("st", "s", str(s)))
        pipe.lrange(p("st", "lp"), 0, -1)
        pipe.lrange(p("st", "lb"), 0, -1)
        pipe.set(p("st", "t0"), f"{now:.3f}", nx=True)
        pipe.get(p("st", "t0"))
        res = await pipe.execute()
        counters = {k.decode(): int(v) for k, v in res[0].items()}
        rejected = {k.decode(): int(v) for k, v in res[1].items()}
        buckets = res[2 : 2 + len(secs)]
        started = float(res[-1])
        covered = max(1.0, min(now - secs[0], now - started))

        def rate(name: str) -> float:
            return round(sum(int(b.get(name.encode(), 0)) for b in buckets) / covered, 2)

        return {
            "uptime_s": round(now - started, 3),
            "packets_total": counters.get("packets", 0),
            "packets_per_s": rate("packets"),
            "rejected_total": rejected,
            "matches_total": counters.get("matches", 0),
            "matches_per_s": rate("matches"),
            "processing_latency_ms": percentiles(float(x) for x in res[-4]),
            "backsignal_latency_ms": percentiles(float(x) for x in res[-3]),
            "counters": counters,
        }
