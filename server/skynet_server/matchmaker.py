"""Stereo matchmaker (blueprint section 3).

For each telemetry packet the pipeline has already stored the detection and
found the active neighbours within ``d_max``. Here, for every neighbour:

1. pick candidate lines of sight near the packet time: when the neighbour has
   two samples of the same local track bracketing the packet time (closer
   together than ``interp_max_gap``), its line of sight is interpolated to the
   packet time (normalised lerp of the ECEF unit vectors, accurate for the
   small angles swept between frames); otherwise the nearest sample within
   ``window`` (50 ms) is used;
2. triangulate each candidate against the packet's ray and gate it
   (``triangulate_rays`` + ``gate`` from skynet_common), behind a cheap
   closest-approach pre-filter that discards most wrong pairings before the
   geodetic conversion;
3. keep the candidate with the smallest normalised miss distance.

With two or more accepted neighbours all rays are solved together by
weighted least squares, and rays whose residual is inconsistent with the
angular noise model are dropped one at a time (worst first).

Guided search: in a dense network a packet has tens of neighbours, each
reporting several objects, so a full search costs hundreds of candidate
tests. Once a local track has been matched, the matchmaker remembers which
local track each neighbour reported for the same object ("partners") and
tests only those, falling back to the full search every
``full_search_interval`` (to pick up nodes that start seeing the object) and
immediately whenever the guided search finds nothing. The cache is
per-process and purely an optimisation: every result still passes the same
gate, and a cold cache (e.g. a fresh serverless instance) just means full
searches.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from skynet_common.geodesy import Vec3, angle_between_deg, ecef_to_enu_vector, enu_to_azel
from skynet_common.triangulation import Ray, RayIntersection, gate, triangulate_rays

from .config import MatchConfig
from .fastmath import miss_prefilter, position_sigma_m, triangulate_many_fast
from .models import Detection, Match, RayObs

# (origin, direction, before, after, weight): ``after`` is None for a raw sample.
Candidate = Tuple[Vec3, Vec3, Detection, Optional[Detection], float]

_PARTNER_TTL_NS = 3_000_000_000
_CACHE_SWEEP_EVERY = 20_000


def pair_key(node_a: str, seq_a: int, node_b: str, seq_b: int) -> str:
    """Order-independent identity of a pair of detections."""
    if node_a <= node_b:
        return f"{node_a}:{seq_a}|{node_b}:{seq_b}"
    return f"{node_b}:{seq_b}|{node_a}:{seq_a}"


def ray_of(d: Detection) -> RayObs:
    return RayObs(d.node_id, d.seq, d.track_id, d.lat, d.lon, d.alt_m, d.az_deg, d.el_deg, d.origin, d.direction)


def candidate_ray(c: Candidate) -> RayObs:
    """Materialise a candidate as a RayObs (az/el recomputed if interpolated)."""
    origin, direction, before, after, w = c
    if after is None:
        return ray_of(before)
    lat = before.lat + (after.lat - before.lat) * w
    lon = before.lon + (after.lon - before.lon) * w
    az, el = enu_to_azel(*ecef_to_enu_vector(lat, lon, direction))
    nearest = before if w <= 0.5 else after
    return RayObs(before.node_id, nearest.seq, before.track_id, lat, lon,
                  before.alt_m + (after.alt_m - before.alt_m) * w, az, el, origin, direction, interpolated=True)


class _Partners:
    __slots__ = ("by_node", "full_ns")

    def __init__(self) -> None:
        self.by_node: Dict[str, Tuple[str, int]] = {}  # neighbour -> (its local track, last seen t_ns)
        self.full_ns = -(1 << 62)


class Matchmaker:
    def __init__(self, cfg: MatchConfig, full_search_interval_ns: int = 1_000_000_000):
        self.cfg = cfg
        self.gate_cfg = cfg.gate
        self.sigma_rad = math.radians(cfg.gate.angular_sigma_deg)
        # How far around the packet time neighbour detections are needed.
        self.span_ns = max(cfg.window_ns, cfg.interp_max_gap_ns)
        self.full_search_interval_ns = full_search_interval_ns
        self._partners: Dict[Tuple[str, str], _Partners] = {}
        self._calls = 0
        self.full_searches = 0
        self.guided_searches = 0

    def windows(self, detections: Iterable[Detection], neighbours: Dict[str, List[str]]
                ) -> Dict[str, Tuple[int, int]]:
        """Union time window per neighbour node over all packets in a batch."""
        out: Dict[str, Tuple[int, int]] = {}
        span = self.span_ns
        for d in detections:
            lo, hi = d.t_ns - span, d.t_ns + span
            for nb in neighbours.get(d.node_id, ()):
                w = out.get(nb)
                out[nb] = (lo, hi) if w is None else (min(w[0], lo), max(w[1], hi))
        return out

    def candidates(self, t_ns: int, dets: Sequence[Detection], only_track: Optional[str] = None
                   ) -> List[Candidate]:
        """Candidate lines of sight of one neighbour at time ``t_ns``.

        ``dets`` are that neighbour's detections, oldest first; with
        ``only_track`` only samples of that local track are considered.
        """
        window, gap = self.cfg.window_ns, self.cfg.interp_max_gap_ns
        out: List[Candidate] = []
        brackets: Dict[str, List[Optional[Detection]]] = {}
        for x in dets:
            tid = x.track_id
            if only_track is not None and tid != only_track:
                continue
            if tid is None:
                if -window <= x.t_ns - t_ns <= window:
                    out.append((x.origin, x.direction, x, None, 0.0))
                continue
            entry = brackets.get(tid)
            if entry is None:
                entry = brackets[tid] = [None, None]
            if x.t_ns <= t_ns:
                entry[0] = x
            elif entry[1] is None:
                entry[1] = x
        for before, after in brackets.values():
            if before is not None and before.t_ns == t_ns:
                out.append((before.origin, before.direction, before, None, 0.0))
                continue
            if before is not None and after is not None and after.t_ns - before.t_ns <= gap:
                w = (t_ns - before.t_ns) / (after.t_ns - before.t_ns)
                a, b = before.direction, after.direction
                dx, dy, dz = a[0] + (b[0] - a[0]) * w, a[1] + (b[1] - a[1]) * w, a[2] + (b[2] - a[2]) * w
                n = math.sqrt(dx * dx + dy * dy + dz * dz)
                oa, ob = before.origin, after.origin
                origin = (oa[0] + (ob[0] - oa[0]) * w, oa[1] + (ob[1] - oa[1]) * w, oa[2] + (ob[2] - oa[2]) * w)
                out.append((origin, (dx / n, dy / n, dz / n), before, after, w))
                continue
            nearest = None
            for x in (before, after):
                if x is not None and abs(x.t_ns - t_ns) <= window:
                    if nearest is None or abs(x.t_ns - t_ns) < abs(nearest.t_ns - t_ns):
                        nearest = x
            if nearest is not None:
                out.append((nearest.origin, nearest.direction, nearest, None, 0.0))
        return out

    def _best_for(self, d: Detection, ray_a: Ray, cands: List[Candidate]
                  ) -> Optional[Tuple[float, Candidate, RayIntersection]]:
        g = self.gate_cfg
        sigma, k, floor, min_r, max_r = self.sigma_rad, g.k_sigma, g.miss_floor_m, g.min_range_m, g.max_range_m
        top: Optional[Tuple[float, Candidate, RayIntersection]] = None
        for cand in cands:
            if not miss_prefilter(d.origin, d.direction, cand[0], cand[1], sigma, k, floor, min_r, max_r):
                continue
            ix, _ = triangulate_rays(ray_a, Ray(cand[0], cand[1]))
            if ix is None:
                continue
            ok, nmiss, _ = gate(ix, g)
            if ok and (top is None or nmiss < top[0]):
                top = (nmiss, cand, ix)
        return top

    def _search(self, d: Detection, neighbour_ids: Sequence[str], nb_dets: Dict[str, List[Detection]],
                guide: Optional[Dict[str, Tuple[str, int]]]) -> List[Tuple[float, Candidate, RayIntersection]]:
        ray_a = Ray(d.origin, d.direction)
        best = []
        if guide is None:
            for nb in neighbour_ids:
                dets = nb_dets.get(nb)
                if dets:
                    top = self._best_for(d, ray_a, self.candidates(d.t_ns, dets))
                    if top is not None:
                        best.append(top)
        else:
            for nb, (tid, _) in guide.items():
                dets = nb_dets.get(nb)
                if dets:
                    top = self._best_for(d, ray_a, self.candidates(d.t_ns, dets, only_track=tid))
                    if top is not None:
                        best.append(top)
        return best

    def match(self, d: Detection, neighbour_ids: Sequence[str], nb_dets: Dict[str, List[Detection]]
              ) -> Optional[Match]:
        self._calls += 1
        if self._calls % _CACHE_SWEEP_EVERY == 0:
            self._sweep(d.t_ns)
        partners = None
        if d.track_id is not None:
            key = (d.node_id, d.track_id)
            partners = self._partners.get(key)
            if partners is None:
                partners = self._partners[key] = _Partners()
        best = []
        if partners is not None and partners.by_node and d.t_ns - partners.full_ns < self.full_search_interval_ns:
            live = {nb: p for nb, p in partners.by_node.items() if d.t_ns - p[1] <= _PARTNER_TTL_NS}
            if live:
                self.guided_searches += 1
                best = self._search(d, neighbour_ids, nb_dets, live)
        if not best:
            self.full_searches += 1
            if partners is not None:
                partners.full_ns = d.t_ns
            best = self._search(d, neighbour_ids, nb_dets, None)
        if not best:
            return None
        best.sort(key=lambda item: item[0])
        me = ray_of(d)
        result = None
        if len(best) >= 2:
            result = self._solve_many(d.t_ns, me, [candidate_ray(c) for _, c, _ in best])
        if result is None:
            _, cand, ix = best[0]
            other = candidate_ray(cand)
            g = self.gate_cfg
            result = Match(
                t_ns=d.t_ns,
                rays=[me, other],
                point=ix.point,
                lat=ix.lat_deg,
                lon=ix.lon_deg,
                alt_m=ix.alt_m,
                sigma_m=position_sigma_m((d.direction, other.direction), (ix.range_a_m, ix.range_b_m),
                                         g.angular_sigma_deg),
                error_margin_m=ix.error_margin_m,
                parallax_deg=ix.parallax_deg,
                pair_keys=[pair_key(d.node_id, d.seq, other.node_id, other.seq)],
            )
        if partners is not None:
            for r in result.rays[1:]:
                if r.track_id is not None:
                    partners.by_node[r.node_id] = (r.track_id, d.t_ns)
        return result

    def _sweep(self, now_t_ns: int) -> None:
        stale = [k for k, p in self._partners.items()
                 if not p.by_node or max(t for _, t in p.by_node.values()) < now_t_ns - 10 * _PARTNER_TTL_NS]
        for k in stale:
            del self._partners[k]

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
