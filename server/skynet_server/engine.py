"""The ingest pipeline shared by the UDP listener and ``POST /api/v1/ingest``.

One call to ``Engine.process`` handles a batch of raw messages (one drained
UDP receive buffer or one NDJSON request):

    decode + verify -> freshness -> neighbours -> search plans -> admit
    (replay drop, node upsert, store detection, fetch neighbour detections)
    -> match -> claim pairs -> link/create 3D tracks -> clip requests ->
    commit -> UDP back-signal -> persistence

Batching is what makes the Redis backend viable at high rates: each arrow
that touches state is at most one pipelined round trip per batch, not per
packet. With the memory backend nothing awaits, so a batch runs to completion
in one event-loop step.

Clip requests to UDP nodes are sent from inside this call, right after the
commit that makes the clip record visible to the upload endpoints; the
receipt-to-``sendto`` time is the back-signal latency in ``/stats``.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence, Set, Tuple

from skynet_common.geodesy import azel_to_ecef_unit, geodetic_to_ecef
from skynet_common.protocol import Heartbeat, ProtocolError, Telemetry, encode_signed

from .config import Settings
from .decode import Decoder, Message
from .live import EventHub
from .matchmaker import Matchmaker, Plan
from .models import ClipRecord, Commit, Detection, Match, NodeRecord, NodeUpdate, TrackState, track_point
from .persistence import BatchWriter, Database, DbBatch
from .state.base import Admission, StateBackend, UdpAddr
from .stats import Stats
from .tracks import TrackManager, TrackUpdate

log = logging.getLogger("skynet.engine")

PAIR_PREFIX = "p:"
_UDP_SHARE_REFRESH_NS = 20_000_000_000


class DatagramSender(Protocol):
    def sendto(self, data: bytes, addr: UdpAddr) -> None: ...


@dataclass(slots=True)
class Inbound:
    data: bytes
    recv_ns: int  # wall clock at receipt (freshness check)
    recv_perf_ns: int  # perf_counter_ns at receipt (latency measurement)
    addr: Optional[UdpAddr] = None


@dataclass
class BatchOutcome:
    reasons: List[Optional[str]]  # per item, None = accepted
    accepted: int = 0
    node_ids: Set[str] = field(default_factory=set)  # authenticated senders
    matches: int = 0


@dataclass(slots=True)
class _Pending:
    index: int
    msg: Message
    admission: Admission
    plan: Optional[Plan] = None


class Engine:
    def __init__(self, settings: Settings, state: StateBackend, stats: Stats, hub: EventHub,
                 db: Optional[Database] = None, writer: Optional[BatchWriter] = None):
        self.settings = settings
        self.state = state
        self.stats = stats
        self.hub = hub
        self.db = db
        self.writer = writer
        self.decoder = Decoder(settings.master_key, not settings.allow_unsigned, settings.freshness_ns)
        self.matchmaker = Matchmaker(settings.match)
        self.tracks = TrackManager(settings.tracks)
        self.shared_state = state.name != "memory"
        self.transport: Optional[DatagramSender] = None  # set by the UDP listener
        self._udp_book: Dict[str, Tuple[UdpAddr, int]] = {}
        self._udp_shared: Dict[str, Tuple[UdpAddr, int]] = {}

    # --- helpers -------------------------------------------------------------

    def base_url(self, request_base: Optional[str] = None) -> str:
        if self.settings.public_url:
            return self.settings.public_url
        if request_base:
            return request_base.rstrip("/")
        host = self.settings.http_host if self.settings.http_host not in ("0.0.0.0", "::", "") else "127.0.0.1"
        return f"http://{host}:{self.settings.http_port}"

    def public_node(self, node: NodeRecord, now_ns: Optional[int] = None) -> Dict[str, Any]:
        return node.public(self.settings.public_location_decimals, now_ns or time.time_ns(),
                           self.settings.node_active_ns)

    async def emit(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Append events produced outside the ingest path (API actions)."""
        stored = await self.state.append_events(events)
        self.hub.publish(stored)
        return stored

    async def persist(self, batch: DbBatch) -> None:
        """Queue (standalone) or write (vercel: before the response) a batch."""
        if self.db is None or batch.is_empty():
            return
        if self.writer is not None:
            self.writer.submit(batch)
            return
        try:
            await self.db.write(batch)
        except Exception:  # noqa: BLE001 - the live pipeline must not fail on DB errors
            self.stats.count("db_errors")
            log.exception("database write failed")

    @staticmethod
    def _convert(msg: Message) -> Admission:
        g, o = msg.gps_coordinates, msg.imu_orientation
        if isinstance(msg, Telemetry):
            v = msg.vector_target
            det = Detection(
                msg.node_id, msg.timestamp_ns, msg.seq, msg.track_id, g.latitude, g.longitude, g.altitude_m,
                v.azimuth_deg, v.elevation_deg,
                geodetic_to_ecef(g.latitude, g.longitude, g.altitude_m),
                azel_to_ecef_unit(g.latitude, g.longitude, v.azimuth_deg, v.elevation_deg),
            )
            update = NodeUpdate(msg.node_id, msg.timestamp_ns, g.latitude, g.longitude, g.altitude_m,
                                o.yaw_deg, o.pitch_deg, o.roll_deg)
            return Admission(update, msg.seq, det)
        assert isinstance(msg, Heartbeat)
        update = NodeUpdate(msg.node_id, msg.timestamp_ns, g.latitude, g.longitude, g.altitude_m, o.yaw_deg,
                            o.pitch_deg, o.roll_deg, msg.camera.fov_h_deg, msg.camera.fov_v_deg, msg.status,
                            msg.software_version)
        return Admission(update, msg.seq, None)

    def _note_udp(self, node_id: str, addr: UdpAddr, now_ns: int, commit: Commit) -> None:
        self._udp_book[node_id] = (addr, now_ns)
        if self.shared_state:
            shared = self._udp_shared.get(node_id)
            if shared is None or shared[0] != addr or now_ns - shared[1] > _UDP_SHARE_REFRESH_NS:
                self._udp_shared[node_id] = (addr, now_ns)
                commit.udp_addrs[node_id] = (addr, now_ns)

    # --- the pipeline ----------------------------------------------------------

    async def process(self, items: Sequence[Inbound], request_base: Optional[str] = None) -> BatchOutcome:
        st = self.stats
        out = BatchOutcome(reasons=[None] * len(items))
        now_ns = time.time_ns()
        pending: List[_Pending] = []
        for i, item in enumerate(items):
            try:
                msg = self.decoder.decode(item.data)
            except ProtocolError as exc:
                out.reasons[i] = exc.reason
                st.reject(exc.reason)
                continue
            out.node_ids.add(msg.node_id)
            if not self.decoder.is_fresh(msg, item.recv_ns):
                out.reasons[i] = "stale"
                st.reject("stale")
                continue
            pending.append(_Pending(i, msg, self._convert(msg)))

        commit = Commit()
        db = DbBatch()
        outgoing: List[Tuple[ClipRecord, bytes, int]] = []
        if pending:
            outgoing = await self._ingest(items, pending, out, commit, db, now_ns, request_base)

        udp_sends: List[Tuple[bytes, UdpAddr, int]] = []
        queued_perf: List[int] = []
        if outgoing:
            udp_sends, queued_perf = await self._route_clip_requests(outgoing, commit, db, now_ns)

        stats_delta = st.drain() if st.shared else None
        if not commit.is_empty() or (stats_delta is not None and not stats_delta.is_empty()):
            stored = await self.state.commit(commit, self.settings.tracks.link_ttl_ms, stats_delta)
            self.hub.publish(stored)

        if udp_sends:
            transport = self.transport
            assert transport is not None
            for data, addr, recv_perf in udp_sends:
                transport.sendto(data, addr)
                st.backsignal_ms((time.perf_counter_ns() - recv_perf) / 1e6)
            st.count("clip_requests_udp", len(udp_sends))
        if queued_perf:
            done = time.perf_counter_ns()
            for recv_perf in queued_perf:
                st.backsignal_ms((done - recv_perf) / 1e6)
            st.count("clip_requests_queued", len(queued_perf))

        await self.persist(db)
        end = time.perf_counter_ns()
        for item in items:
            st.processing_ms((end - item.recv_perf_ns) / 1e6)
        return out

    async def _ingest(self, items: Sequence[Inbound], pending: List[_Pending], out: BatchOutcome,
                      commit: Commit, db: DbBatch, now_ns: int, request_base: Optional[str]
                      ) -> List[Tuple[ClipRecord, bytes, int]]:
        st = self.stats
        mm = self.matchmaker
        reporters = {p.msg.node_id: (p.admission.update.lat, p.admission.update.lon)
                     for p in pending if p.admission.detection is not None}
        neighbours = await self.state.neighbours(reporters, self.settings.match.radius_m, now_ns) if reporters else {}
        for p in pending:
            d = p.admission.detection
            if d is not None:
                p.plan = mm.plan(d, neighbours.get(d.node_id, ()))
        windows = None
        if self.state.prefetch:
            windows = mm.windows(p.plan for p in pending if p.plan is not None)
        admit = await self.state.admit([p.admission for p in pending], now_ns, windows)

        plans: List[Tuple[Plan, int]] = []
        telemetry = 0
        for p, fresh in zip(pending, admit.fresh):
            if not fresh:
                out.reasons[p.index] = "replay"
                st.reject("replay")
                continue
            out.accepted += 1
            item = items[p.index]
            if item.addr is not None:
                # Only after the replay check: a replayed datagram from another
                # address must not redirect the node's back-signals.
                self._note_udp(p.msg.node_id, item.addr, now_ns, commit)
            d = p.admission.detection
            if d is not None:
                telemetry += 1
                if self.settings.store_detections:
                    db.detections.append(d)
                if p.plan is not None:
                    plans.append((p.plan, item.recv_perf_ns))
        st.count("packets", out.accepted)
        st.count("telemetry", telemetry)
        st.count("heartbeats", len(admit.nodes))
        for node in admit.nodes.values():
            commit.events.append({"type": "node", "t_ns": now_ns, "data": self.public_node(node, now_ns)})
            db.nodes[node.node_id] = node

        found: List[Match] = []
        retry: List[Tuple[Plan, int]] = []
        for plan, recv_perf in plans:
            m = mm.match(plan, admit.source)
            if m is not None:
                m.recv_perf_ns = recv_perf
                found.append(m)
            elif plan.retry:
                retry.append((plan.as_full(), recv_perf))
        if retry:
            if self.state.prefetch:
                await self.state.fetch_more(admit.source, mm.windows(p for p, _ in retry))
            for plan, recv_perf in retry:
                m = mm.match(plan, admit.source)
                if m is not None:
                    m.recv_perf_ns = recv_perf
                    found.append(m)
        if not found:
            return []
        matches = await self._claim(found)
        if not matches:
            return []
        out.matches = len(matches)
        st.count("matches", len(matches))
        return await self._link_tracks(matches, commit, db, now_ns, request_base)

    async def _claim(self, found: List[Match]) -> List[Match]:
        """Keep a match only if every detection pair it contributes is new: of
        several packets (or instances) that see the same simultaneous pair,
        exactly one emits it."""
        keys = [PAIR_PREFIX + k for m in found for k in m.pair_keys]
        claimed = await self.state.claim(keys, self.settings.match.pair_ttl_ms)
        emitted: List[Match] = []
        pos = 0
        for m in found:
            n = len(m.pair_keys)
            if all(claimed[pos : pos + n]):
                emitted.append(m)
            pos += n
        return emitted

    async def _link_tracks(self, matches: List[Match], commit: Commit, db: DbBatch, now_ns: int,
                           request_base: Optional[str]) -> List[Tuple[ClipRecord, bytes, int]]:
        keys = sorted({k for m in matches for k in self.tracks.link_keys(m)})
        links = await self.state.get_links(keys) if keys else {}
        tracks: Dict[str, TrackState] = {}
        if links:
            tracks = await self.state.get_tracks(sorted(set(links.values())))
        updates = self.tracks.assign(matches, links, tracks, now_ns)
        decimals = self.settings.public_location_decimals
        base: Optional[str] = None
        outgoing: List[Tuple[ClipRecord, bytes, int]] = []
        track_events: Dict[str, TrackState] = {}
        created = 0
        for m, upd in zip(matches, updates):
            if upd is None:  # vetoed ghost pairing
                self.stats.count("matches_vetoed")
                continue
            track = upd.track
            created += upd.created
            commit.tracks[track.track_id] = track
            for k in self.tracks.link_keys(m):
                commit.links[k] = track.track_id
            if upd.emit_event:
                track_events[track.track_id] = track
            if self.db is not None:
                db.tracks[track.track_id] = track
            event_id = secrets.token_hex(8)
            targets = self._clip_targets(upd, m)
            if targets:
                base = base or self.base_url(request_base)
                for node_id in targets:
                    clip, signed = self._clip_request(track.track_id, event_id, node_id, m.t_ns, base)
                    outgoing.append((clip, signed, m.recv_perf_ns))
            if not upd.recorded:
                self.stats.count("matches_refinement_skipped")
                continue
            node_ids = [r.node_id for r in m.rays]
            data = {
                "event_id": event_id,
                "track_id": track.track_id,
                "t_ns": m.t_ns,
                "lat": round(m.lat, 7),
                "lon": round(m.lon, 7),
                "alt_m": round(m.alt_m, 2),
                "sigma_m": round(m.sigma_m, 2),
                "error_margin_m": round(m.error_margin_m, 2),
                "parallax_deg": round(m.parallax_deg, 3),
                "rays": [
                    {"node_id": r.node_id, "az_deg": round(r.az_deg, 4), "el_deg": round(r.el_deg, 4),
                     "node_lat": round(r.lat, decimals), "node_lon": round(r.lon, decimals),
                     "node_alt_m": round(r.alt_m, 1)}
                    for r in m.rays
                ],
            }
            commit.events.append({"type": "triangulation", "t_ns": m.t_ns, "data": data})
            commit.new_points.setdefault(track.track_id, []).append(
                track_point(m.t_ns, m.lat, m.lon, m.alt_m, m.sigma_m, m.error_margin_m, node_ids))
            if self.db is not None:
                db.triangulations.append({
                    **data,
                    "node_ids": node_ids,
                    # Full-precision rays for the durable record.
                    "rays": [{"node_id": r.node_id, "seq": r.seq, "track_id": r.track_id, "az_deg": r.az_deg,
                              "el_deg": r.el_deg, "lat": r.lat, "lon": r.lon, "alt_m": r.alt_m,
                              "interpolated": r.interpolated} for r in m.rays],
                })
        for track in track_events.values():
            commit.events.append({"type": "track", "t_ns": track.last_ns, "data": track.public()})
        self.stats.count("tracks_created", created)
        return outgoing

    @staticmethod
    def _clip_targets(upd: TrackUpdate, m: Match) -> List[str]:
        """One clip request per (track, node): all contributors when the track
        is created, afterwards each node that newly joins it."""
        track = upd.track
        wanted = [r.node_id for r in m.rays] if upd.created else upd.new_nodes
        targets = [n for n in wanted if n not in track.clip_nodes]
        track.clip_nodes.extend(targets)
        return targets

    def _clip_request(self, track_id: str, event_id: str, node_id: str, t_ns: int, base: str
                      ) -> Tuple[ClipRecord, bytes]:
        request_id = secrets.token_hex(12)
        issued = time.time_ns()
        w = self.settings.clip_window_ns
        clip = ClipRecord(request_id, event_id, track_id, node_id, t_ns - w, t_ns + w, issued)
        msg = {
            "type": "clip_request",
            "request_id": request_id,
            "event_id": event_id,
            "node_id": node_id,
            "t_start_ns": clip.t_start_ns,
            "t_end_ns": clip.t_end_ns,
            "upload_url": f"{base}/api/v1/clips/{request_id}/upload-target",
            "issued_ns": issued,
        }
        return clip, encode_signed(msg, self.decoder.node_key(node_id))

    async def _route_clip_requests(self, outgoing: List[Tuple[ClipRecord, bytes, int]], commit: Commit,
                                   db: DbBatch, now_ns: int) -> Tuple[List[Tuple[bytes, UdpAddr, int]], List[int]]:
        """Decide per request: UDP datagram (the node has a fresh UDP return
        address and this process has a UDP socket) or the HTTPS command queue."""
        horizon = now_ns - self.settings.node_active_ns
        addrs: Dict[str, UdpAddr] = {}
        if self.transport is not None:
            missing: List[str] = []
            for clip, _, _ in outgoing:
                entry = self._udp_book.get(clip.node_id)
                if entry is not None and entry[1] >= horizon:
                    addrs[clip.node_id] = entry[0]
                elif self.shared_state:
                    missing.append(clip.node_id)
            if missing:
                for node_id, (addr, seen_ns) in (await self.state.get_udp_addrs(missing)).items():
                    if seen_ns >= horizon:
                        addrs[node_id] = addr
        udp: List[Tuple[bytes, UdpAddr, int]] = []
        queued: List[int] = []
        for clip, signed, recv_perf in outgoing:
            commit.clips.append(clip)
            db.clips[clip.request_id] = clip
            commit.events.append({"type": "clip", "t_ns": clip.issued_ns, "data": {
                "request_id": clip.request_id, "event_id": clip.event_id, "track_id": clip.track_id,
                "node_id": clip.node_id, "status": "requested", "download_url": None}})
            addr = addrs.get(clip.node_id)
            if addr is not None:
                udp.append((signed, addr, recv_perf))
            else:
                commit.commands.setdefault(clip.node_id, []).append(signed.decode())
                queued.append(recv_perf)
        return udp, queued
