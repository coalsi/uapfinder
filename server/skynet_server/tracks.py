"""3D tracks: linking triangulations, kinematics and classification.

Linking: each node reports a local ``track_id`` per object, so a 3D track is
the set of triangulations whose rays share a (node_id, local track) link.
A new triangulation joins an existing 3D track if any of its rays' local
tracks is already linked (the one with most points wins if several are);
otherwise a new 3D track is created. Links expire after ``link_ttl``
without updates.

Kinematics: triangulated points are noisy (sigma tens of metres at typical
ranges), so velocity and acceleration come from weighted least-squares
polynomial fits with one outlier-rejection pass, never from finite
differences. The window length is set by what the fit can resolve:

* speed: a line over the last ~1 s (sigma_v ~ 20 m/s at 10 Hz and 20 m);
* acceleration: a quadratic over the last ~3 s at full rate. Over 1 s the
  acceleration error would be ~100 m/s^2, the size of the 10 g threshold
  itself; over 3 s it is ~10 m/s^2 (sigma_a scales as T^-2.5);
* ballistic flight and stationarity: a 1 Hz decimated history of the last
  30 s (line and quadratic).

Each fit yields standard errors (scaled up by the reduced chi-square when
the scatter exceeds the error model). Flags are evaluated on conservative
bounds, because a track is re-fitted ten times a second and the largest of
hundreds of noisy estimates is far from typical: ``hypersonic`` needs
speed - 3 sigma_v > 1715 m/s; ``high_g`` needs |a| - 3.5 sigma_a > 98 m/s^2
from a fit with sigma_a <= 25 m/s^2 (the reported ``max_accel_mps2`` is that
lower bound); ``stationary`` needs speed + 2 sigma_v < 2 m/s over >= 29 s.
``ballistic`` (not numerically defined in the API) is free fall: over
>= 5 s, at >= 100 m/s, the acceleration matches local gravity within
max(3 sigma_a, 2 m/s^2) with sigma_a <= 1.5 m/s^2, so that it is clearly
distinguished from constant velocity (|a| >= g/2). Drag and the Coriolis
term (< 0.3 m/s^2 below Mach 6) are ignored. Flags are sticky over the
track's life; ``classification`` is the highest-priority fired flag.
"""

from __future__ import annotations

import bisect
import math
import secrets
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from skynet_common.geodesy import EARTH_MEAN_RADIUS_M, enu_basis

from .config import TrackConfig
from .models import Match, TrackState, track_point

HYPERSONIC_MPS = 1715.0
HIGH_G_MPS2 = 98.0
STATIONARY_MPS = 2.0
STATIONARY_SPAN_S = 30.0
G0 = 9.80665
FLAG_ORDER = ("hypersonic", "high_g", "ballistic", "stationary")

_HISTORY_SLOT_S = 1.0
_HISTORY_SPAN_S = 31.0
_BALLISTIC_SPAN_S = 10.0
_FIT_INTERVAL_NS = 250_000_000
_LONG_FIT_INTERVAL_NS = 1_000_000_000
_SPEED_WINDOW_S = 1.0
_ACCEL_MIN_POINTS = 12
_ACCEL_MIN_SPAN_S = 2.0
_ACCEL_MAX_SIGMA = HIGH_G_MPS2 / 4.0
SAME_FRAME_NS = 20_000_000
REFINE_RATIO = 1.25
_OUTLIER_SIGMA = 3.5

Sample = Tuple[float, float, float, float, float]  # t_s, x, y, z, sigma_m


def _sample_time(sample: Tuple[int, float, float, float, float]) -> int:
    return sample[0]


@dataclass
class Fit:
    velocity: Tuple[float, float, float]
    sigma_v: float
    accel: Optional[Tuple[float, float, float]] = None
    sigma_a: float = math.inf
    span_s: float = 0.0
    n: int = 0

    @property
    def speed(self) -> float:
        vx, vy, vz = self.velocity
        return math.sqrt(vx * vx + vy * vy + vz * vz)

    @property
    def accel_mag(self) -> float:
        if self.accel is None:
            return 0.0
        ax, ay, az = self.accel
        return math.sqrt(ax * ax + ay * ay + az * az)


def _fit_once(samples: Sequence[Sample], quadratic: bool) -> Optional[Tuple[Fit, List[float]]]:
    """Weighted least squares per ECEF axis; returns the fit and each
    sample's normalised 3D residual (|residual| / sample sigma)."""
    n = len(samples)
    t_ref = samples[-1][0]
    x_ref, y_ref, z_ref = samples[-1][1], samples[-1][2], samples[-1][3]
    s0 = s1 = s2 = s3 = s4 = 0.0
    r = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]  # [axis][power] sums of w t^p y
    rows = []
    for t_s, x, y, z, sig in samples:
        t = t_s - t_ref
        w = 3.0 / max(sig * sig, 1e-6)
        wt = w * t
        wt2 = wt * t
        s0 += w
        s1 += wt
        s2 += wt2
        s3 += wt2 * t
        s4 += wt2 * t * t
        dy = (x - x_ref, y - y_ref, z - z_ref)
        for axis in range(3):
            v = dy[axis]
            r[axis][0] += w * v
            r[axis][1] += wt * v
            r[axis][2] += wt2 * v
        rows.append((t, w, dy))
    span = samples[-1][0] - samples[0][0]
    if span <= 0.0:
        return None
    coeffs: List[Tuple[float, float, float]] = []
    if not quadratic:
        det = s0 * s2 - s1 * s1
        if det <= 0.0:
            return None
        for axis in range(3):
            sy, sty = r[axis][0], r[axis][1]
            coeffs.append(((s2 * sy - s1 * sty) / det, (s0 * sty - s1 * sy) / det, 0.0))
        var_v, var_c2 = s0 / det, math.inf
        n_params = 2
    else:
        # Inverse of the symmetric moment matrix [[s0,s1,s2],[s1,s2,s3],[s2,s3,s4]].
        c00 = s2 * s4 - s3 * s3
        c01 = s2 * s3 - s1 * s4
        c02 = s1 * s3 - s2 * s2
        c11 = s0 * s4 - s2 * s2
        c12 = s1 * s2 - s0 * s3
        c22 = s0 * s2 - s1 * s1
        det = s0 * c00 + s1 * c01 + s2 * c02
        if det <= 0.0 or c22 <= 0.0:
            return None
        for axis in range(3):
            b0, b1, b2 = r[axis]
            coeffs.append(((c00 * b0 + c01 * b1 + c02 * b2) / det,
                           (c01 * b0 + c11 * b1 + c12 * b2) / det,
                           (c02 * b0 + c12 * b1 + c22 * b2) / det))
        var_v, var_c2 = c11 / det, c22 / det
        n_params = 3
    chi2 = 0.0
    normalised = []
    for t, w, dy in rows:
        e2 = 0.0
        for axis in range(3):
            c0, c1, c2 = coeffs[axis]
            e = dy[axis] - (c0 + c1 * t + c2 * t * t)
            e2 += e * e
        chi2 += w * e2
        normalised.append(math.sqrt(w * e2 / 3.0))
    dof = 3 * n - 3 * n_params
    scale = max(1.0, chi2 / dof) if dof > 0 else 1.0
    fit = Fit(
        velocity=(coeffs[0][1], coeffs[1][1], coeffs[2][1]),
        sigma_v=math.sqrt(var_v * scale),
        span_s=span,
        n=n,
    )
    if quadratic:
        fit.accel = (2.0 * coeffs[0][2], 2.0 * coeffs[1][2], 2.0 * coeffs[2][2])
        fit.sigma_a = 2.0 * math.sqrt(var_c2 * scale)
    return fit, normalised


def fit_polynomial(samples: Sequence[Sample], quadratic: bool) -> Optional[Fit]:
    """Weighted least-squares fit of position vs time, per ECEF axis.

    Time is measured from the latest sample, so the fitted velocity and
    acceleration refer to that instant. Each sample's ``sigma_m`` is a 3D RMS
    error, i.e. sigma^2/3 per axis, giving weights 3/sigma^2. Samples more
    than ``_OUTLIER_SIGMA`` from the first fit (in units of their own sigma
    and the fit's RMS scatter, whichever is larger) are dropped once and the
    fit is repeated, so one bad association cannot fake a manoeuvre.
    """
    min_n = 4 if quadratic else 3
    if len(samples) < min_n:
        return None
    first = _fit_once(samples, quadratic)
    if first is None:
        return None
    fit, normalised = first
    rms = math.sqrt(sum(x * x for x in normalised) / len(normalised))
    limit = _OUTLIER_SIGMA * max(1.0, rms)
    kept = [s for s, x in zip(samples, normalised) if x <= limit]
    if len(kept) == len(samples) or len(kept) < min_n:
        return fit
    again = _fit_once(kept, quadratic)
    return again[0] if again is not None else fit


def gravity_ecef(lat_deg: float, lon_deg: float, alt_m: float) -> Tuple[float, float, float]:
    _, _, up = enu_basis(lat_deg, lon_deg)
    g = G0 * (EARTH_MEAN_RADIUS_M / (EARTH_MEAN_RADIUS_M + alt_m)) ** 2
    return (-g * up[0], -g * up[1], -g * up[2])


def new_track_id() -> str:
    return "T-" + secrets.token_hex(5)


@dataclass
class TrackUpdate:
    track: TrackState
    created: bool
    new_nodes: List[str]
    emit_event: bool
    recorded: bool = True  # False: a same-frame refinement too small to keep as a point


class TrackManager:
    def __init__(self, cfg: TrackConfig):
        self.cfg = cfg
        self.vetoed = 0

    @staticmethod
    def link_keys(m: Match) -> List[str]:
        return [f"{r.node_id}|{r.track_id}" for r in m.rays if r.track_id is not None]

    def assign(self, matches: Sequence[Match], links: Dict[str, str], tracks: Dict[str, TrackState],
               now_ns: int) -> List[Optional[TrackUpdate]]:
        """Attach each match to a 3D track, creating tracks as needed.

        The rays' links vote for a track (ties go to the track with more
        points), and the winner must also be kinematically consistent with
        the new point. A single mismatched ("ghost") ray therefore cannot pull
        a triangulation into another object's track, and since every ray of
        the match is then linked to the winner, a ghost link is overwritten
        by the next correct majority.

        Ghost veto: a two-ray match whose rays belong only to established
        tracks it is inconsistent with is almost always a wrong pairing made
        before the packet's true partners arrived (two rays always "meet"
        somewhere within the gate when the sky is busy), so it is dropped
        (None) instead of spawning a spurious track. Matches of three or more
        mutually consistent rays are geometrically self-validating and always
        kept. ``links`` and ``tracks`` are updated in place so later matches
        in the same batch see earlier ones.
        """
        out: List[Optional[TrackUpdate]] = []
        for m in matches:
            keys = self.link_keys(m)
            votes: Dict[str, int] = {}
            for k in keys:
                tid = links.get(k)
                if tid is not None and tid in tracks:
                    votes[tid] = votes.get(tid, 0) + 1
            track = None
            for tid in sorted(votes, key=lambda t: (votes[t], tracks[t].point_count), reverse=True):
                if self.consistent(tracks[tid], m):
                    track = tracks[tid]
                    break
            if track is None and votes and len(m.rays) == 2:
                self.vetoed += 1
                out.append(None)
                continue
            created = track is None
            if track is None:
                track = TrackState(track_id=new_track_id(), created_ns=now_ns, first_ns=m.t_ns, last_ns=m.t_ns)
                tracks[track.track_id] = track
            for k in keys:
                links[k] = track.track_id
            out.append(self.update(track, m, created, now_ns))
        return out

    @staticmethod
    def consistent(track: TrackState, m: Match) -> bool:
        """Could the track's object have moved to the new point in time?

        Generous on purpose (300 m/s and 3-sigma margins on top of the
        estimated speed, 8 km/s when the speed is not yet known): it only
        has to separate distinct objects, not police manoeuvres.
        """
        recent = track.recent
        if not recent:
            return True
        i = bisect.bisect_left(recent, m.t_ns, key=_sample_time)  # recent is time-sorted
        if i == len(recent) or (i > 0 and m.t_ns - recent[i - 1][0] <= recent[i][0] - m.t_ns):
            i -= 1
        t, x, y, z, sig = recent[i]
        dt = abs(m.t_ns - t) / 1e9
        px, py, pz = m.point
        dist = math.sqrt((px - x) ** 2 + (py - y) ** 2 + (pz - z) ** 2)
        speed = 1.5 * track.speed_mps + 300.0 if track.speed_mps is not None else 8000.0
        return dist <= speed * dt + 3.0 * (sig + m.sigma_m) + 100.0

    @staticmethod
    def redundant(track: TrackState, m: Match) -> bool:
        """Is ``m`` a same-frame refinement not worth a point of its own?

        A frame seen by k nodes yields up to k-1 triangulations as its packets
        arrive, each with one more ray. Keeping only those that improve on the
        frame's best sigma by at least ``1 - 1/REFINE_RATIO`` (20%) leaves
        about log2(k) points per frame instead of k-1, without losing the most
        precise solutions.
        """
        for t, _, _, _, sigma in reversed(track.recent):
            if abs(t - m.t_ns) < SAME_FRAME_NS and sigma <= REFINE_RATIO * m.sigma_m:
                return True
            if t < m.t_ns - SAME_FRAME_NS:
                break
        return False

    def update(self, track: TrackState, m: Match, created: bool, now_ns: int) -> TrackUpdate:
        node_ids = [r.node_id for r in m.rays]
        new_nodes = [n for n in node_ids if n not in track.node_ids]
        track.node_ids.extend(new_nodes)
        recorded = created or not self.redundant(track, m)
        changed = False
        if recorded:
            track.point_count += 1
            track.first_ns = min(track.first_ns, m.t_ns)
            if m.t_ns >= track.last_ns or track.last_point is None:
                track.last_ns = m.t_ns
                track.last_point = track_point(m.t_ns, m.lat, m.lon, m.alt_m, m.sigma_m, m.error_margin_m,
                                               node_ids)
            self._add_sample(track, m)
            before = (track.classification, tuple(track.flags))
            short = created or abs(m.t_ns - track.last_fit_ns) >= _FIT_INTERVAL_NS
            long = abs(m.t_ns - track.last_long_fit_ns) >= _LONG_FIT_INTERVAL_NS
            if short or long:
                if short:
                    track.last_fit_ns = m.t_ns
                if long:
                    track.last_long_fit_ns = m.t_ns
                self._update_kinematics(track, short, long)
            changed = (track.classification, tuple(track.flags)) != before
        emit = created or changed or bool(new_nodes) or m.t_ns - track.last_event_ns >= self.cfg.event_interval_ns
        if emit:
            track.last_event_ns = m.t_ns
        return TrackUpdate(track, created, new_nodes, emit, recorded)

    def _add_sample(self, track: TrackState, m: Match) -> None:
        """Add the point to the fit windows.

        A frame seen by k nodes yields up to k-1 triangulations of nearly the
        same instant from nested ray subsets; their errors are strongly
        correlated, so only the most precise one per ``SAME_FRAME_NS`` is
        kept for the fits. A sample enters the decimated history once a
        later frame has superseded it.
        """
        x, y, z = m.point
        sample = (m.t_ns, x, y, z, m.sigma_m)
        recent = track.recent
        i = len(recent)
        while i > 0 and recent[i - 1][0] > m.t_ns:
            i -= 1
        for j in (i - 1, i):
            if 0 <= j < len(recent) and abs(recent[j][0] - m.t_ns) < SAME_FRAME_NS:
                if m.sigma_m < recent[j][4]:
                    recent[j] = sample
                return
        if i == len(recent) and recent:
            self._add_history(track, recent[-1])
        recent.insert(i, sample)
        cutoff = recent[-1][0] - self.cfg.fit_window_ns
        drop = 0
        while drop < len(recent) and recent[drop][0] < cutoff:
            drop += 1
        drop = max(drop, len(recent) - self.cfg.max_recent_points)
        if drop:
            del recent[:drop]

    @staticmethod
    def _add_history(track: TrackState, sample: Tuple[int, float, float, float, float]) -> None:
        t_ns, x, y, z, sigma = sample
        t_s = (t_ns - track.created_ns) / 1e9
        slot = math.floor(t_s / _HISTORY_SLOT_S)
        hist = track.history
        entry = next((h for h in reversed(hist) if h[0] == slot), None)
        if entry is None:
            hist.append([slot, 1, t_s, x, y, z, sigma * sigma])
            hist.sort(key=lambda h: h[0])
            newest = hist[-1][0]
            while hist and hist[0][0] < newest - _HISTORY_SPAN_S / _HISTORY_SLOT_S:
                hist.pop(0)
        else:
            entry[1] += 1
            entry[2] += t_s
            entry[3] += x
            entry[4] += y
            entry[5] += z
            entry[6] += sigma * sigma

    @staticmethod
    def _history_samples(track: TrackState, span_s: float) -> List[Sample]:
        out: List[Sample] = []
        if not track.history:
            return out
        newest_t = track.history[-1][2] / track.history[-1][1]
        for slot, n, st, sx, sy, sz, sv in track.history:
            t = st / n
            if t >= newest_t - span_s:
                # The mean of n samples has variance (mean variance) / n.
                out.append((t, sx / n, sy / n, sz / n, math.sqrt(sv / n / n)))
        return out

    def _update_kinematics(self, track: TrackState, short: bool, long: bool) -> None:
        """Refit the full-rate windows (``short``) and/or the decimated history
        (``long``, which only changes once per second) and update the flags."""
        line = quad = long_line = long_quad = None
        if short:
            recent = [((t - track.created_ns) / 1e9, x, y, z, s) for t, x, y, z, s in track.recent]
            last_s = recent[-1][0] if recent else 0.0
            line = fit_polynomial([x for x in recent if x[0] >= last_s - _SPEED_WINDOW_S], quadratic=False)
            quad = fit_polynomial(recent, quadratic=True) if len(recent) >= _ACCEL_MIN_POINTS else None
        if long:
            long_samples = self._history_samples(track, STATIONARY_SPAN_S)
            long_line = fit_polynomial(long_samples, quadratic=False) if len(long_samples) >= 3 else None
            ballistic_samples = self._history_samples(track, _BALLISTIC_SPAN_S)
            long_quad = fit_polynomial(ballistic_samples, quadratic=True) if len(ballistic_samples) >= 5 else None

        current = line if line is not None and line.n >= 5 and line.span_s >= 0.4 else long_line
        if current is not None:
            track.speed_mps = current.speed
            track.max_speed_mps = max(track.max_speed_mps, current.speed)
        flags = set(track.flags)
        for fit in (line, long_line):
            if fit is not None and fit.n >= 5 and fit.speed - 3.0 * fit.sigma_v > HYPERSONIC_MPS:
                flags.add("hypersonic")
        for fit, min_span in ((quad, _ACCEL_MIN_SPAN_S), (long_quad, 4.0)):
            if (fit is not None and fit.span_s >= min_span and fit.sigma_a <= _ACCEL_MAX_SIGMA
                    and fit.accel is not None):
                track.max_accel_mps2 = max(track.max_accel_mps2, fit.accel_mag - 3.5 * fit.sigma_a)
        if track.max_accel_mps2 > HIGH_G_MPS2:
            flags.add("high_g")
        if (long_line is not None and long_line.span_s >= STATIONARY_SPAN_S - 1.0
                and long_line.speed + 2.0 * long_line.sigma_v < STATIONARY_MPS):
            flags.add("stationary")
        if long_quad is not None and long_quad.accel is not None and long_quad.span_s >= 5.0 and track.last_point:
            lp = track.last_point
            g = gravity_ecef(lp["lat"], lp["lon"], lp["alt_m"])
            a = long_quad.accel
            mismatch = math.sqrt(sum((a[i] - g[i]) ** 2 for i in range(3)))
            if (long_quad.sigma_a <= 1.5 and long_quad.speed >= 100.0 and long_quad.accel_mag >= G0 / 2.0
                    and mismatch <= max(3.0 * long_quad.sigma_a, 2.0)):
                flags.add("ballistic")
        track.flags = [f for f in FLAG_ORDER if f in flags]
        track.classification = track.flags[0] if track.flags else "unknown"
