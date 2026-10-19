"""Stereo matchmaker (blueprint section 3).

For each telemetry packet the pipeline has already stored the detection and
found the active neighbours within ``d_max``. For every neighbour searched:

1. pick candidate lines of sight near the packet time: when the neighbour has
   two samples of the same local track bracketing the packet time (closer
   together than ``interp_max_gap``), its line of sight is interpolated to the
   packet time (normalised lerp of the ECEF unit vectors, accurate for the
   small angles swept between frames); otherwise the nearest sample within
   ``window`` (50 ms) is used;
2. gate each candidate against the packet's ray (``fast_gate``: the
   ``gate(triangulate_rays(...))`` decision without the geodetic conversion);
3. keep the candidate with the smallest normalised miss distance.

With two or more accepted neighbours all rays (the best ``max_rays - 1``
neighbours plus the packet's own) are solved together by weighted least squares, and rays whose residual is inconsistent with the
angular noise model are dropped one at a time (worst first). Otherwise, or
if fewer than three consistent rays remain, the best pair is triangulated
with the reference ``triangulate_rays`` and must pass the reference ``gate``.

Search plans. In a dense network a packet has tens of neighbours, each
reporting several objects, so a full search costs hundreds of candidate
tests. Once a local track has been matched, the matchmaker remembers which
local track each other node reported for the same object ("partners", learnt
in both directions from every match) and a *guided* search tests only those.
A node that starts seeing a known object finds the established observers
with its own full search and teaches them about itself, so the periodic full
search of an established track (every ``full_search_interval``) is only a
safety net. A guided search that finds nothing falls back to a full one only
when its partners have gone silent for a second or reported rays that fail
the gate, not merely because they have not reported this frame yet (or their
packets were shed under overload). A local track whose last full search found nothing and that has no
partners is not searched again for ``miss_backoff``: its neighbours' own
searches will find it as soon as they see the object too, and teach it its
partners. The cache is per process and purely an optimisation: every result
passes the same gate, and a cold cache (a fresh serverless instance) just
means full searches.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from skynet_common.geodesy import Vec3, angle_between_deg
from skynet_common.triangulation import Ray, gate, triangulate_rays

from .config import MatchConfig
from .fastmath import fast_gate, position_sigma_m, ray_meets_cone, triangulate_many_fast
from .models import Detection, Match, RayObs
from .tracks import REFINE_RATIO, SAME_FRAME_NS

# (origin, direction, before, after, weight): ``after`` is None for a raw sample.
Candidate = Tuple[Vec3, Vec3, Detection, Optional[Detection], float]
Window = Tuple[int, int]

_PARTNER_TTL_NS = 3_000_000_000
# A partner silent this long has lost the object (or renamed its track). Shorter
# gaps are usually lost or late packets and do not justify a full search.
_SILENT_NS = 1_000_000_000
_CACHE_SWEEP_EVERY = 20_000


#: One local track's samples: (timestamps, detections), both oldest first.
TrackSamples = Tuple[List[int], List[Detection]]


class DetectionSource(Protocol):
    """Recent detections of other nodes, as the matchmaker reads them."""

    def local_track(self, node_id: str, track_id: str) -> Optional[TrackSamples]:
        """Samples of one local track of a node, if any."""

    def local_tracks(self, node_id: str) -> Iterable[TrackSamples]:
        """Samples of every local track of a node."""

    def untracked(self, node_id: str, lo_ns: int, hi_ns: int) -> Sequence[Detection]:
        """Detections without a local track id with lo_ns <= t_ns <= hi_ns."""


class NodeSamples:
    """One node's recent detections, grouped by local track, each group
    time-sorted; ``add`` ignores a detection it already holds and trims
    samples older than ``cutoff_ns``."""

    __slots__ = ("tracks", "loose", "newest_ns")

    def __init__(self) -> None:
        self.tracks: Dict[str, TrackSamples] = {}
        self.loose: TrackSamples = ([], [])  # detections without a track id
        self.newest_ns = 0

    def add(self, d: Detection, cutoff_ns: int) -> None:
        if d.track_id is None:
            entry = self.loose
        else:
            entry = self.tracks.get(d.track_id)  # type: ignore[assignment]
            if entry is None:
                entry = self.tracks[d.track_id] = ([], [])
        ts, dets = entry
        i = len(ts) if not ts or d.t_ns > ts[-1] else bisect.bisect_right(ts, d.t_ns)
        j = i - 1
        while j >= 0 and ts[j] == d.t_ns:
            if dets[j].seq == d.seq:
                return  # the same detection seen twice (e.g. fetched again)
            j -= 1
        ts.insert(i, d.t_ns)
        dets.insert(i, d)
        if ts[0] < cutoff_ns:
            cut = bisect.bisect_left(ts, cutoff_ns)
            del ts[:cut]
            del dets[:cut]
        if d.t_ns > self.newest_ns:
            self.newest_ns = d.t_ns

    def prune(self, cutoff_ns: int) -> None:
        """Drop local tracks whose newest sample is older than ``cutoff_ns``."""
        for tid in [t for t, (ts, _) in self.tracks.items() if ts[-1] < cutoff_ns]:
            del self.tracks[tid]
        ts, dets = self.loose
        if ts and ts[0] < cutoff_ns:
            cut = bisect.bisect_left(ts, cutoff_ns)
            del ts[:cut]
            del dets[:cut]

    def untracked(self, lo_ns: int, hi_ns: int) -> Sequence[Detection]:
        ts, dets = self.loose
        if not ts:
            return ()
        return dets[bisect.bisect_left(ts, lo_ns) : bisect.bisect_right(ts, hi_ns)]


@dataclass(slots=True)
class Plan:
    """How one packet will be searched: ``guide`` maps partner node -> its
    local track (guided search) or is None (full search over ``neighbours``).
    ``windows`` tells prefetching backends what to fetch for it."""

    detection: Detection
    neighbours: Sequence[str]
    guide: Optional[Dict[str, str]] = None
    # Set by a guided search that found nothing when a full search might
    # still succeed: the partners were silent, or their candidates failed
    # the gate (a partner's local track may now follow another object).
    # Partners that simply have not reported this frame yet do not count.
    retry: bool = False



def pair_key(node_a: str, seq_a: int, node_b: str, seq_b: int) -> str:
    """Order-independent identity of a pair of detections."""
    if node_a <= node_b:
        return f"{node_a}:{seq_a}|{node_b}:{seq_b}"
    return f"{node_b}:{seq_b}|{node_a}:{seq_a}"


def ray_of(d: Detection) -> RayObs:
    return RayObs(d.node_id, d.seq, d.track_id, d.lat, d.lon, d.alt_m, d.az_deg, d.el_deg, d.origin, d.direction)


def candidate_ray(c: Candidate) -> RayObs:
    """Materialise a candidate as a RayObs (az/el derived lazily if interpolated)."""
    origin, direction, before, after, w = c
    if after is None:
        return ray_of(before)
    nearest = before if w <= 0.5 else after
    return RayObs(before.node_id, nearest.seq, before.track_id, before.lat + (after.lat - before.lat) * w,
                  before.lon + (after.lon - before.lon) * w, before.alt_m + (after.alt_m - before.alt_m) * w,
                  None, None, origin, direction, interpolated=True)


class _Partners:
    __slots__ = ("by_node", "full_ns", "miss_ns")

    def __init__(self) -> None:
        self.by_node: Dict[str, Tuple[str, int]] = {}  # other node -> (its local track, last matched t_ns)
        self.full_ns = -(1 << 62)  # packet time of the last full search
        self.miss_ns = -(1 << 62)  # packet time of the last full search that found nothing


#: node -> (camera position, optical axis, cosine of the half-angle that
#: contains the whole field of view), all ECEF.
Cones = Dict[str, Tuple[Vec3, Vec3, float]]


class Matchmaker:
    def __init__(self, cfg: MatchConfig, full_search_interval_ns: int = 10_000_000_000,
                 miss_backoff_ns: int = 300_000_000, cones: Optional[Cones] = None):
        self.cfg = cfg
        # Camera cones of the nodes; a full search skips neighbours whose
        # camera cannot see any point of the packet's line of sight.
        self.cones: Cones = cones if cones is not None else {}
        self.gate_cfg = cfg.gate
        self.sigma_rad = math.radians(cfg.gate.angular_sigma_deg)
        # How far around the packet time neighbour detections are needed.
        self.span_ns = max(cfg.window_ns, cfg.interp_max_gap_ns)
        self.full_search_interval_ns = full_search_interval_ns
        self.miss_backoff_ns = miss_backoff_ns
        self._partners: Dict[Tuple[str, str], _Partners] = {}
        # local track -> (t_ns, ECEF point, sigma_m) of the latest recorded
        # solution it took part in (see ``note_recorded``).
        self._frames: Dict[Tuple[str, str], Tuple[int, Vec3, float]] = {}
        self._calls = 0
        self.full_searches = 0
        self.guided_searches = 0
        self.skipped = 0
        self.refinements_skipped = 0
        self.cone_skipped = 0

    # --- planning ----------------------------------------------------------------

    def plan(self, d: Detection, neighbours: Sequence[str]) -> Optional[Plan]:
        """Decide how to search a packet; None means no search at all."""
        self._calls += 1
        if self._calls % _CACHE_SWEEP_EVERY == 0:
            self._sweep(d.t_ns)
        if not neighbours:
            return None
        if d.track_id is None:
            return self.full_plan(d, neighbours)
        p = self._partners.get((d.node_id, d.track_id))
        if p is None:
            return self.full_plan(d, neighbours)
        t = d.t_ns
        live = [(seen, nb, tid) for nb, (tid, seen) in p.by_node.items() if t - seen <= _PARTNER_TTL_NS]
        if live and t - p.full_ns < self.full_search_interval_ns:
            if len(live) >= self.cfg.max_rays:
                live.sort(reverse=True)  # most recently matched first
                del live[self.cfg.max_rays - 1 :]
            return Plan(d, neighbours, {nb: tid for _, nb, tid in live})
        if not live and 0 <= t - p.miss_ns < self.miss_backoff_ns:
            self.skipped += 1
            return None
        return self.full_plan(d, neighbours)

    def full_plan(self, d: Detection, neighbours: Sequence[str]) -> Plan:
        """A full search over the neighbours that could have seen along the
        packet's line of sight (nodes without known camera geometry are kept)."""
        cones = self.cones
        if not cones:
            return Plan(d, neighbours)
        g = self.gate_cfg
        o, v = d.origin, d.direction
        kept = []
        for nb in neighbours:
            cone = cones.get(nb)
            if cone is None or ray_meets_cone(o, v, cone[0], cone[1], cone[2], g.min_range_m, g.max_range_m):
                kept.append(nb)
        self.cone_skipped += len(neighbours) - len(kept)
        return Plan(d, kept)

    def windows(self, plans: Iterable[Plan]) -> Dict[str, Window]:
        """Union time window per node over the nodes the plans will search."""
        out: Dict[str, Window] = {}
        span = self.span_ns
        for plan in plans:
            t = plan.detection.t_ns
            lo, hi = t - span, t + span
            for nb in (plan.guide if plan.guide is not None else plan.neighbours):
                w = out.get(nb)
                out[nb] = (lo, hi) if w is None else (min(w[0], lo), max(w[1], hi))
        return out

    # --- candidates ----------------------------------------------------------------

    def track_candidate(self, t_ns: int, samples: TrackSamples) -> Optional[Candidate]:
        """One local track's line of sight at ``t_ns``: the exact sample, the
        interpolation between the samples bracketing ``t_ns`` (if they are at
        most ``interp_max_gap`` apart), or the nearest sample within
        ``window``; None if there is no usable sample."""
        ts, dets = samples
        i = bisect.bisect_right(ts, t_ns)
        before = dets[i - 1] if i else None
        if before is not None and before.t_ns == t_ns:
            return (before.origin, before.direction, before, None, 0.0)
        after = dets[i] if i < len(dets) else None
        if before is not None and after is not None and after.t_ns - before.t_ns <= self.cfg.interp_max_gap_ns:
            w = (t_ns - before.t_ns) / (after.t_ns - before.t_ns)
            a, b = before.direction, after.direction
            dx, dy, dz = a[0] + (b[0] - a[0]) * w, a[1] + (b[1] - a[1]) * w, a[2] + (b[2] - a[2]) * w
            n = math.sqrt(dx * dx + dy * dy + dz * dz)
            oa, ob = before.origin, after.origin
            origin = (oa[0] + (ob[0] - oa[0]) * w, oa[1] + (ob[1] - oa[1]) * w, oa[2] + (ob[2] - oa[2]) * w)
            return (origin, (dx / n, dy / n, dz / n), before, after, w)
        window = self.cfg.window_ns
        db = t_ns - before.t_ns if before is not None else window + 1
        da = after.t_ns - t_ns if after is not None else window + 1
        if db <= window and db <= da:
            return (before.origin, before.direction, before, None, 0.0)  # type: ignore[union-attr]
        if da <= window:
            return (after.origin, after.direction, after, None, 0.0)  # type: ignore[union-attr]
        return None

    def candidates(self, t_ns: int, node_id: str, source: DetectionSource) -> List[Candidate]:
        """Every candidate line of sight of one node at time ``t_ns``."""
        out = []
        window = self.cfg.window_ns
        stale = t_ns - window
        for samples in source.local_tracks(node_id):
            # Without a sample in the last window and nothing later, a track
            # cannot give a candidate: skip it without the bracket search.
            if samples[0][-1] < stale:
                continue
            c = self.track_candidate(t_ns, samples)
            if c is not None:
                out.append(c)
        for x in source.untracked(node_id, t_ns - window, t_ns + window):
            out.append((x.origin, x.direction, x, None, 0.0))
        return out

    def _best_for(self, d: Detection, cands: List[Candidate]) -> Optional[Tuple[float, Candidate]]:
        """The accepted candidate with the smallest normalised miss distance."""
        sigma, g = self.sigma_rad, self.gate_cfg
        top: Optional[Tuple[float, Candidate]] = None
        for cand in cands:
            nmiss = fast_gate(d.origin, d.direction, cand[0], cand[1], sigma, g)
            if nmiss is not None and (top is None or nmiss < top[0]):
                top = (nmiss, cand)
        return top

    # --- matching ----------------------------------------------------------------

    def match(self, plan: Plan, source: DetectionSource) -> Optional[Match]:
        """Search one packet. A guided search that finds nothing returns None
        without side effects; when ``plan.retry`` is then set, the caller
        should search again with ``full_plan``."""
        d = plan.detection
        t = d.t_ns
        best: List[Tuple[float, Candidate]] = []
        sigma, g = self.sigma_rad, self.gate_cfg
        if plan.guide is not None:
            self.guided_searches += 1
            heard = rejected = False
            for nb, tid in plan.guide.items():
                samples = source.local_track(nb, tid)
                if samples is None or t - samples[0][-1] > _SILENT_NS:
                    continue  # the partner has stopped reporting this track
                heard = True
                cand = self.track_candidate(t, samples)
                if cand is None:
                    continue  # not reported for this frame yet
                nmiss = fast_gate(d.origin, d.direction, cand[0], cand[1], sigma, g)
                if nmiss is None:
                    rejected = True
                else:
                    best.append((nmiss, cand))
            plan.retry = not best and (rejected or not heard)
        else:
            self.full_searches += 1
            for nb in plan.neighbours:
                top = self._best_for(d, self.candidates(t, nb, source))
                if top is not None:
                    best.append(top)
            if d.track_id is not None:
                p = self._partner_entry(d.node_id, d.track_id)
                p.full_ns = d.t_ns
                if not best:
                    p.miss_ns = d.t_ns
        if not best:
            return None
        best.sort(key=lambda item: item[0])
        del best[self.cfg.max_rays - 1 :]
        agreed = self._predictably_redundant(d, best)
        if agreed is not None:
            self.refinements_skipped += 1
            self._learn_tracks(d, agreed, t)
            return None
        me = ray_of(d)
        result = None
        if len(best) >= 2:
            result = self._solve_many(d.t_ns, me, [candidate_ray(c) for _, c in best])
        if result is None:
            result = self._best_pair(d, me, best)
        if result is not None:
            self._learn(result.rays, d.t_ns)
        return result

    def _best_pair(self, d: Detection, me: RayObs, best: List[Tuple[float, Candidate]]) -> Optional[Match]:
        """Two-ray result from the best candidate that passes the exact gate."""
        ray_a = Ray(d.origin, d.direction)
        for _, cand in best:
            ix, _ = triangulate_rays(ray_a, Ray(cand[0], cand[1]))
            if ix is None or not gate(ix, self.gate_cfg)[0]:
                continue
            other = candidate_ray(cand)
            return Match(
                t_ns=d.t_ns,
                rays=[me, other],
                point=ix.point,
                lat=ix.lat_deg,
                lon=ix.lon_deg,
                alt_m=ix.alt_m,
                sigma_m=position_sigma_m((d.direction, other.direction), (ix.range_a_m, ix.range_b_m),
                                         self.gate_cfg.angular_sigma_deg),
                error_margin_m=ix.error_margin_m,
                parallax_deg=ix.parallax_deg,
                pair_keys=[pair_key(d.node_id, d.seq, other.node_id, other.seq)],
            )
        return None

    def note_recorded(self, m: Match) -> None:
        """Remember a solution that was kept as a track point (engine callback)."""
        entry = (m.t_ns, m.point, m.sigma_m)
        for r in m.rays:
            if r.track_id is not None:
                self._frames[(r.node_id, r.track_id)] = entry

    def _predictably_redundant(self, d: Detection, best: List[Tuple[float, Candidate]]) -> Optional[List[Detection]]:
        """Would solving this packet only produce a same-frame refinement that
        the track manager discards (``TrackManager.redundant``)?

        Applies when a partner already took part in a recorded solution of
        this frame and the packet's own ray passes within the noise bound of
        that solution's point. The new sigma is then predicted from the
        closed-form covariance at that point, which costs a fraction of the
        least-squares solve; the track manager still applies the exact rule
        to whatever is solved. Returns the partners that took part in the
        reference solution (safe to learn from) when the solve can be
        skipped, else None.
        """
        ref: Optional[Tuple[int, Vec3, float]] = None
        for _, cand in best:
            before = cand[2]
            if before.track_id is None:
                continue
            f = self._frames.get((before.node_id, before.track_id))
            if f is not None and abs(f[0] - d.t_ns) < SAME_FRAME_NS and (ref is None or f[2] < ref[2]):
                ref = f
        if ref is None:
            return None
        g = self.gate_cfg
        px, py, pz = ref[1]
        ox, oy, oz = d.origin
        vx, vy, vz = d.direction
        ex, ey, ez = px - ox, py - oy, pz - oz
        rng = ex * vx + ey * vy + ez * vz
        perp = math.sqrt(max(ex * ex + ey * ey + ez * ez - rng * rng, 0.0))
        if rng <= 0.0 or perp > g.k_sigma * rng * self.sigma_rad + g.miss_floor_m:
            return None  # this ray does not agree with the frame's solution: solve properly
        dirs = [d.direction]
        ranges = [rng]
        for _, (o, v, _, _, _) in best:
            dirs.append(v)
            ranges.append(math.sqrt((px - o[0]) ** 2 + (py - o[1]) ** 2 + (pz - o[2]) ** 2))
        if ref[2] > REFINE_RATIO * position_sigma_m(dirs, ranges, g.angular_sigma_deg):
            return None
        return [c[2] for _, c in best
                if c[2].track_id is not None and self._frames.get((c[2].node_id, c[2].track_id)) == ref]

    def _learn_tracks(self, d: Detection, others: Sequence[Detection], t_ns: int) -> None:
        """``_learn`` for a packet whose solve was skipped."""
        if d.track_id is None:
            return
        mine = self._partner_entry(d.node_id, d.track_id).by_node
        for o in others:
            if o.track_id is not None and o.node_id != d.node_id:
                mine[o.node_id] = (o.track_id, t_ns)
                self._partner_entry(o.node_id, o.track_id).by_node[d.node_id] = (d.track_id, t_ns)

    def _partner_entry(self, node_id: str, track_id: str) -> _Partners:
        p = self._partners.get((node_id, track_id))
        if p is None:
            p = self._partners[(node_id, track_id)] = _Partners()
        return p

    def _learn(self, rays: Sequence[RayObs], t_ns: int) -> None:
        """The packet's local track (``rays[0]``) and every other ray's local
        track become partners of each other. Linear in the ray count; the
        other pairs are learnt from those nodes' own matches."""
        me = rays[0]
        if me.track_id is None:
            return
        mine = self._partner_entry(me.node_id, me.track_id).by_node
        for r in rays[1:]:
            if r.track_id is not None and r.node_id != me.node_id:
                mine[r.node_id] = (r.track_id, t_ns)
                self._partner_entry(r.node_id, r.track_id).by_node[me.node_id] = (me.track_id, t_ns)

    def _sweep(self, now_t_ns: int) -> None:
        horizon = now_t_ns - 10 * _PARTNER_TTL_NS
        stale = [k for k, p in self._partners.items()
                 if max(p.full_ns, p.miss_ns, max((t for _, t in p.by_node.values()), default=p.full_ns)) < horizon]
        for k in stale:
            del self._partners[k]
        for k in [k for k, f in self._frames.items() if f[0] < horizon]:
            del self._frames[k]

    def _solve_many(self, t_ns: int, me: RayObs, others: List[RayObs]) -> Optional[Match]:
        """Least-squares solution of all rays, dropping inconsistent ones.

        ``others`` is sorted best-first, so when the solve fails outright the
        weakest candidates are removed first. Returns None when fewer than
        three mutually consistent rays remain (the caller falls back to the
        best pair).
        """
        g = self.gate_cfg
        sigma, k, floor = self.sigma_rad, g.k_sigma, g.miss_floor_m
        rays = [me] + list(others)
        while len(rays) >= 3:
            sol = triangulate_many_fast([Ray(r.origin, r.direction) for r in rays], g.angular_sigma_deg)
            if sol is None:
                rays.pop()
                continue
            worst_i, worst_ratio = 0, 0.0
            for i in range(1, len(rays)):
                ratio = sol.residuals_m[i] / (k * sol.ranges_m[i] * sigma + floor)
                if ratio > worst_ratio:
                    worst_i, worst_ratio = i, ratio
            if worst_ratio > 1.0:
                rays.pop(worst_i)
                continue
            if not (g.min_alt_m <= sol.alt_m <= g.max_alt_m):
                return None
            return Match(
                t_ns=t_ns,
                rays=rays,
                point=sol.point,
                lat=sol.lat_deg,
                lon=sol.lon_deg,
                alt_m=sol.alt_m,
                sigma_m=position_sigma_m([r.direction for r in rays], sol.ranges_m, g.angular_sigma_deg),
                # For two rays the closest-approach distance is twice the RMS
                # perpendicular residual; the same scaling keeps the metric
                # comparable for N rays.
                error_margin_m=2.0 * sol.rms_residual_m,
                parallax_deg=_max_parallax_deg(rays),
                pair_keys=[pair_key(me.node_id, me.seq, r.node_id, r.seq) for r in rays[1:]],
            )
        return None


def _max_parallax_deg(rays: Sequence[RayObs]) -> float:
    """Largest angle between any two rays (the pair with the smallest dot product)."""
    lowest, pair = 2.0, (0, 1)
    for i in range(len(rays)):
        a = rays[i].direction
        for j in range(i + 1, len(rays)):
            b = rays[j].direction
            dp = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
            if dp < lowest:
                lowest, pair = dp, (i, j)
    return angle_between_deg(rays[pair[0]].direction, rays[pair[1]].direction)
