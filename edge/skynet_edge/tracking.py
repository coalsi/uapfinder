"""Multi-object tracking in the image plane.

Each track runs a constant-velocity Kalman filter on the distorted pixel
position (u, v) with white-noise acceleration, which tolerates the turns we
are looking for while still predicting well enough to gate. Detections are
assigned with the Hungarian algorithm on Mahalanobis distances inside a
chi-square gate; the gate graph is split into connected components first,
so the cubic assignment only ever runs on the handful of tracks and blobs
that actually compete with each other.

Lifecycle: *tentative* until ``confirm_hits`` detections within the first
``confirm_window`` frames (random noise almost never does this with a
consistent motion), then *confirmed*; a confirmed track that has had no
measurement for ``max_coast_s`` is *lost* and removed.

Every measurement is stored with its az/el (computed by the caller from the
undistorted centroid and the current attitude), area and intensity, which
is what the kinematics, the false-positive filters and the telemetry use.
"""

from __future__ import annotations

import itertools
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

from .config import TrackingConfig
from .detection import Blob
from .flow import lk_flow

TENTATIVE = "tentative"
CONFIRMED = "confirmed"
LOST = "lost"

_INFEASIBLE = 1e18


@dataclass
class TrackSample:
    t_ns: int
    u: float
    v: float
    az: float
    el: float
    area: int
    peak: float
    snr: float
    eff_area: float
    relative_intensity: float
    polarity: int
    source: str  # "det" (detector) or "lk" (optical-flow bridge)
    bbox: Tuple[int, int, int, int] = (0, 0, 0, 0)  # x, y, w, h of the detection


@dataclass
class Verdict:
    """A false-positive filter's decision about a track."""

    label: str
    reason: str
    sticky_until_ns: int = 0


class KalmanCV:
    """Constant-velocity Kalman filter, state (u, v, du/dt, dv/dt) in px and px/s."""

    def __init__(self, u: float, v: float, vu: float, vv: float, pos_var: float, vel_var: float,
                 accel_psd: float, meas_var: float):
        self.x = np.array([u, v, vu, vv], dtype=np.float64)
        self.P = np.diag([pos_var, pos_var, vel_var, vel_var]).astype(np.float64)
        self.q = accel_psd
        self.R = np.eye(2) * meas_var

    def predict(self, dt: float) -> None:
        if dt <= 0:
            return
        F = np.eye(4)
        F[0, 2] = F[1, 3] = dt
        q = self.q
        a, b, c = q * dt ** 3 / 3.0, q * dt ** 2 / 2.0, q * dt
        Q = np.array([[a, 0, b, 0], [0, a, 0, b], [b, 0, c, 0], [0, b, 0, c]])
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q

    def innovation(self, z: Sequence[float]) -> Tuple[np.ndarray, np.ndarray]:
        y = np.asarray(z, dtype=np.float64) - self.x[:2]
        S = self.P[:2, :2] + self.R
        return y, S

    def mahalanobis2(self, z: Sequence[float]) -> float:
        y, S = self.innovation(z)
        return float(y @ np.linalg.solve(S, y))

    def update(self, z: Sequence[float]) -> None:
        y, S = self.innovation(z)
        K = self.P[:, :2] @ np.linalg.inv(S)
        self.x = self.x + K @ y
        P = self.P - K @ self.P[:2, :]  # (I - K H) P with H = [I 0]
        self.P = 0.5 * (P + P.T)


class Track:
    def __init__(self, track_id: str, kf: KalmanCV, t_ns: int, history_s: float):
        self.id = track_id
        self.kf = kf
        self.state = TENTATIVE
        self.first_ns = t_ns
        self.last_meas_ns = t_ns  # latest sample of any source
        self.last_det_ns = t_ns  # latest detector (not optical-flow) sample
        self.samples: Deque[TrackSample] = deque()
        self.hit_history: List[bool] = []
        self.hits = 0
        self.history_ns = int(history_s * 1e9)
        self.verdicts: Dict[str, Verdict] = {}
        self.flags: Set[str] = set()
        self.kinematics = None  # set by the pipeline (kinematics.Kinematics)
        self.anomaly_score = 0.0
        self.peak_anomaly = 0.0
        self.reported = False
        self.last_report_ns = 0
        self.analyzed_ns = 0  # last time kinematics and filters were evaluated

    @property
    def age_s(self) -> float:
        return (self.last_meas_ns - self.first_ns) / 1e9

    @property
    def last(self) -> TrackSample:
        return self.samples[-1]

    def add_sample(self, s: TrackSample) -> None:
        self.samples.append(s)
        self.last_meas_ns = s.t_ns
        if s.source == "det":
            self.last_det_ns = s.t_ns
        cutoff = s.t_ns - self.history_ns
        while self.samples and self.samples[0].t_ns < cutoff:
            self.samples.popleft()

    def recent(self, window_s: float, sources: Tuple[str, ...] = ("det", "lk")) -> List[TrackSample]:
        """Samples of the last ``window_s`` seconds, oldest first (scans back from the newest)."""
        cutoff = self.last_meas_ns - int(window_s * 1e9)
        out: List[TrackSample] = []
        for s in reversed(self.samples):
            if s.t_ns < cutoff:
                break
            if s.source in sources:
                out.append(s)
        out.reverse()
        return out

    def filtered_by(self) -> Optional[Verdict]:
        return next(iter(self.verdicts.values()), None)


@dataclass
class TrackerOutput:
    active: List[Track] = field(default_factory=list)
    confirmed_now: List[Track] = field(default_factory=list)
    ended: List[Track] = field(default_factory=list)


def hungarian(cost: np.ndarray) -> List[Tuple[int, int]]:
    """Minimum-cost assignment for a dense (n, m) matrix (Kuhn-Munkres with potentials)."""
    n, m = cost.shape
    transposed = n > m
    c = cost.T if transposed else cost
    if transposed:
        n, m = m, n
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = 0
            row = c[i0 - 1]
            for j in range(1, m + 1):
                if not used[j]:
                    cur = row[j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    pairs = [(p[j] - 1, j - 1) for j in range(1, m + 1) if p[j] != 0]
    return [(b, a) for a, b in pairs] if transposed else pairs


def gated_assignment(cost: np.ndarray, feasible: np.ndarray) -> List[Tuple[int, int]]:
    """Optimal assignment restricted to feasible pairs, solved per connected component."""
    n, m = feasible.shape
    parent = list(range(n + m))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    rows, cols = np.nonzero(feasible)
    for r, c in zip(rows.tolist(), cols.tolist()):
        parent[find(r)] = find(n + c)
    groups: Dict[int, Tuple[List[int], List[int]]] = {}
    for r in set(rows.tolist()):
        groups.setdefault(find(r), ([], []))[0].append(r)
    for c in set(cols.tolist()):
        groups.setdefault(find(n + c), ([], []))[1].append(c)
    result: List[Tuple[int, int]] = []
    for rs, cs in groups.values():
        if len(rs) == 1 and len(cs) == 1:
            result.append((rs[0], cs[0]))
            continue
        sub = np.where(feasible[np.ix_(rs, cs)], cost[np.ix_(rs, cs)], _INFEASIBLE)
        for a, b in hungarian(sub):
            if sub[a, b] < _INFEASIBLE:
                result.append((rs[a], cs[b]))
    return result


AzElFn = Callable[[np.ndarray], np.ndarray]


class Tracker:
    def __init__(self, cfg: TrackingConfig, fps: float, id_prefix: Optional[str] = None):
        self.cfg = cfg
        self.fps = fps
        self.tracks: List[Track] = []
        self._ids = itertools.count(1)
        self.prefix = id_prefix or _base36(int(time.time()))
        self._last_t_ns: Optional[int] = None
        self._prev_t_ns: Optional[int] = None

    def _new_id(self) -> str:
        return f"{self.prefix}-{next(self._ids):06d}"

    def update(self, t_ns: int, blobs: Sequence[Blob], to_azel: AzElFn,
               prev_y: Optional[np.ndarray] = None, cur_y: Optional[np.ndarray] = None) -> TrackerOutput:
        cfg = self.cfg
        dt = 0.0 if self._last_t_ns is None else (t_ns - self._last_t_ns) / 1e9
        prev_t = self._last_t_ns
        self._last_t_ns = t_ns
        for tr in self.tracks:
            tr.kf.predict(dt)

        azel = to_azel(np.array([[b.u, b.v] for b in blobs], dtype=np.float64).reshape(-1, 2))
        matched_tracks: Set[int] = set()
        matched_blobs: Set[int] = set()
        if self.tracks and blobs:
            n, m = len(self.tracks), len(blobs)
            cost = np.full((n, m), _INFEASIBLE)
            gate2 = cfg.gate_sigma ** 2
            for i, tr in enumerate(self.tracks):
                pu, pv = tr.kf.x[0], tr.kf.x[1]
                for j, b in enumerate(blobs):
                    if abs(b.u - pu) > cfg.max_gate_px or abs(b.v - pv) > cfg.max_gate_px:
                        continue
                    d2 = tr.kf.mahalanobis2((b.u, b.v))
                    if d2 <= gate2 and math.hypot(b.u - pu, b.v - pv) <= cfg.max_gate_px:
                        cost[i, j] = d2
            for i, j in gated_assignment(cost, cost < _INFEASIBLE):
                tr, b = self.tracks[i], blobs[j]
                tr.kf.update((b.u, b.v))
                tr.add_sample(_sample(t_ns, b, azel[j]))
                matched_tracks.add(i)
                matched_blobs.add(j)

        use_lk = cfg.lk_enabled and prev_y is not None and cur_y is not None and prev_t is not None
        for i, tr in enumerate(self.tracks):
            hit = i in matched_tracks
            if len(tr.hit_history) < cfg.confirm_window:
                tr.hit_history.append(hit)
            if hit:
                tr.hits += 1
                continue
            if use_lk and tr.state == CONFIRMED and tr.samples and tr.last.t_ns == prev_t \
                    and t_ns - tr.last_det_ns <= int(cfg.max_coast_s * 1e9):
                got = lk_flow(prev_y, cur_y, tr.last.u, tr.last.v, cfg.lk_window_px, cfg.lk_levels)
                if got is not None and tr.kf.mahalanobis2(got) <= cfg.gate_sigma ** 2:
                    tr.kf.update(got)
                    tr.add_sample(_bridge_sample(t_ns, tr.last, got, to_azel(np.array([got]))[0]))

        out = TrackerOutput()
        for j, b in enumerate(blobs):
            if j in matched_blobs:
                continue
            tr = self._spawn(t_ns, b, dt, prev_y if use_lk else None, cur_y)
            tr.add_sample(_sample(t_ns, b, azel[j]))
            tr.hits = 1
            tr.hit_history.append(True)
            self.tracks.append(tr)

        survivors: List[Track] = []
        max_coast_ns = int(cfg.max_coast_s * 1e9)
        for tr in self.tracks:
            if tr.state == TENTATIVE:
                if sum(tr.hit_history) >= cfg.confirm_hits:
                    tr.state = CONFIRMED
                    out.confirmed_now.append(tr)
                elif len(tr.hit_history) >= cfg.confirm_window:
                    continue  # never confirmed: discard silently
            if tr.state == CONFIRMED and t_ns - tr.last_det_ns > max_coast_ns:
                tr.state = LOST
                out.ended.append(tr)
                continue
            survivors.append(tr)
        self.tracks = survivors
        out.active = [t for t in survivors if t.state == CONFIRMED]
        return out

    def _spawn(self, t_ns: int, b: Blob, dt: float, prev_y: Optional[np.ndarray],
               cur_y: Optional[np.ndarray]) -> Track:
        cfg = self.cfg
        r2 = cfg.measurement_noise_px ** 2
        vu = vv = 0.0
        vel_var = cfg.initial_velocity_px_s ** 2
        if prev_y is not None and cur_y is not None and dt > 0:
            back = lk_flow(cur_y, prev_y, b.u, b.v, cfg.lk_window_px, cfg.lk_levels)
            if back is not None:
                vu, vv = (b.u - back[0]) / dt, (b.v - back[1]) / dt
                vel_var = (4.0 * cfg.measurement_noise_px / dt) ** 2
        kf = KalmanCV(b.u, b.v, vu, vv, r2, vel_var, cfg.process_accel_px_s2 ** 2, r2)
        return Track(self._new_id(), kf, t_ns, cfg.history_s)


def _sample(t_ns: int, b: Blob, azel: np.ndarray) -> TrackSample:
    return TrackSample(t_ns, b.u, b.v, float(azel[0]), float(azel[1]), b.area, b.peak, b.snr, b.eff_area,
                       b.relative_intensity, b.polarity, "det", b.bbox)


def _bridge_sample(t_ns: int, prev: TrackSample, uv: Tuple[float, float], azel: np.ndarray) -> TrackSample:
    x, y, w, h = prev.bbox
    bbox = (int(round(x + uv[0] - prev.u)), int(round(y + uv[1] - prev.v)), w, h)
    return TrackSample(t_ns, uv[0], uv[1], float(azel[0]), float(azel[1]), prev.area, prev.peak, prev.snr,
                       prev.eff_area, prev.relative_intensity, prev.polarity, "lk", bbox)


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or "0"
