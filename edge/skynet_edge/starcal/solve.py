"""Guided star-field solve: attitude + intrinsics from a stacked night frame.

1. Detect stars (``detect.py``) and compute the apparent az/el of every
   catalog star above the horizon (``astro.py``).
2. Coarse search: for every (pitch, roll, focal length, k1) on a grid around
   the prior, turn the brightest detections into az/el at yaw = 0 and score
   *all* yaws at once against tolerance bitmaps of the brightest catalog
   stars in az/el (a yaw is a pure azimuth shift, so this is one gather per
   detection). The tolerance grows with the distance from the image centre,
   where focal length/distortion errors move stars most.
3. Refine the best distinct candidates: iterative nearest-neighbour matching
   with shrinking radius (one-to-one, blends and photometrically impossible
   pairs rejected) and robust (Huber, 3-sigma clipped) Levenberg-Marquardt on
   pixel residuals for rotation, focal length (fx with a tightly regularised
   fy/fx), principal point (regularised to the centre), k1 and optionally k2.
4. Accept only if enough stars match, the residuals are small, the matched
   stars cover the field, the attitude is well determined, chance matches
   are implausible and no other distinct attitude fits as well.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field, replace
from typing import List, Optional, Sequence, Tuple

import numpy as np

from . import astro
from .catalog import StarCatalog, load_catalog
from .detect import DetectParams, Detections, detect_stars, to_gray_float
from .model import Intrinsics, attitude_matrix, matrix_to_attitude, project_cam, rotvec_matrix, unproject


# --- public data types -----------------------------------------------------

@dataclass(frozen=True)
class Observer:
    """Observer position (WGS84 geodetic, degrees; height above the ellipsoid, m).

    ``pressure_hpa``/``temperature_c`` set the refraction (default: standard
    atmosphere at ``alt_m``, 10 degC). ``dut1_s`` = UT1 - UTC if known.
    """

    lat: float
    lon: float
    alt_m: float = 0.0
    pressure_hpa: Optional[float] = None
    temperature_c: float = 10.0
    dut1_s: float = 0.0

    @property
    def pressure(self) -> float:
        return astro.standard_pressure_hpa(self.alt_m) if self.pressure_hpa is None else self.pressure_hpa


@dataclass(frozen=True)
class Prior:
    """What is known before the solve.

    ``yaw_deg``/``pitch_deg``/``roll_deg``: orientation-sensor reading or
    nominal mount; ``None`` = unknown (searched over the full range).
    ``sigma_deg``: 1-sigma uncertainty of the given angles (None -> 5 deg).
    Per-angle ``*_sigma_deg`` override it. ``fov_h_deg`` is the nominal
    pinhole horizontal field of view, uncertain by ``fov_sigma_frac``.
    Known intrinsics (from a checkerboard) may be given as ``fx``.. ``k2``;
    with ``fit_intrinsics=False`` they are held fixed and only the attitude
    is solved.
    """

    width: int
    height: int
    fov_h_deg: float
    yaw_deg: Optional[float] = None
    pitch_deg: Optional[float] = None
    roll_deg: Optional[float] = None
    sigma_deg: Optional[float] = None
    yaw_sigma_deg: Optional[float] = None
    pitch_sigma_deg: Optional[float] = None
    roll_sigma_deg: Optional[float] = None
    fov_sigma_frac: float = 0.04
    k1_range: Tuple[float, float] = (-0.3, 0.1)
    fx: Optional[float] = None
    fy: Optional[float] = None
    cx: Optional[float] = None
    cy: Optional[float] = None
    k1: Optional[float] = None
    k2: Optional[float] = None
    fit_intrinsics: bool = True

    def sigma(self, which: str) -> Optional[float]:
        if getattr(self, which + "_deg") is None:
            return None
        s = getattr(self, which + "_sigma_deg")
        if s is None:
            s = self.sigma_deg if self.sigma_deg is not None else 5.0
        return float(s)


@dataclass
class StarCalResult:
    ok: bool
    reason: str
    t_ns: int
    yaw_deg: Optional[float] = None
    pitch_deg: Optional[float] = None
    roll_deg: Optional[float] = None
    fx: Optional[float] = None
    fy: Optional[float] = None
    cx: Optional[float] = None
    cy: Optional[float] = None
    k1: Optional[float] = None
    k2: Optional[float] = None
    stars_matched: int = 0
    rms_deg: Optional[float] = None
    # diagnostics
    rms_px: Optional[float] = None
    sigma_deg: Optional[float] = None      # 1-sigma attitude uncertainty (total rotation angle) from the fit
    coverage: Optional[float] = None       # fraction of sky cells of a 4x4 grid with matches
    stars_detected: int = 0
    sun_alt_deg: Optional[float] = None
    moon_alt_deg: Optional[float] = None
    elapsed_s: float = 0.0
    checks: dict = field(default_factory=dict)

    @property
    def dist(self) -> List[float]:
        """OpenCV distortion vector for IntrinsicsConfig.dist."""
        return [self.k1 or 0.0, self.k2 or 0.0, 0.0, 0.0, 0.0]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["dist"] = self.dist
        return d


def sun_altitude_deg(t_ns: int, observer: Observer) -> float:
    return astro.sun_altaz(t_ns, observer.lat, observer.lon)[1]


def is_dark_enough(t_ns: int, observer: Observer, max_sun_alt_deg: float = -12.0) -> bool:
    """True when the Sun's (geometric) altitude is below ``max_sun_alt_deg``."""
    return sun_altitude_deg(t_ns, observer) < max_sun_alt_deg


# --- solver configuration --------------------------------------------------

@dataclass
class SolveParams:
    min_stars: int = 25
    max_rms_deg: float = 0.05
    max_rms_px: float = 1.2
    max_sigma_deg: float = 0.025
    min_coverage: float = 0.4
    min_bright_matched_frac: float = 0.4
    max_sun_alt_deg: float = -6.0
    min_el_deg: float = 3.0           # catalog stars lower than this are not used
    coarse_detections: int = 40
    coarse_res_deg: float = 0.2
    coarse_candidates: int = 4
    coarse_budget: float = 1.5e8      # max lookups in one coarse pass (detections x yaws x combos)
    wide_candidates: int = 10        # first-pass hypotheses re-searched finely for wide/unknown priors
    max_detections: int = 1000
    final_radius_px: float = 3.0
    fit_k2_min_stars: int = 80
    time_budget_s: float = 50.0
    detect: DetectParams = field(default_factory=DetectParams)


# --- helpers ---------------------------------------------------------------

def _fail(t_ns, reason, t0, **kw) -> StarCalResult:
    r = StarCalResult(ok=False, reason=reason, t_ns=t_ns, **kw)
    r.elapsed_s = time.monotonic() - t0
    return r


def _sky_bitmap(az: np.ndarray, el: np.ndarray, tol_deg: float, res: float, el_min: float) -> np.ndarray:
    """Boolean az/el grid: True within ``tol_deg`` (great circle) of any star."""
    n_az = int(round(360.0 / res))
    n_el = int(math.ceil((90.0 - el_min) / res)) + 1
    row_el = el_min + (np.arange(n_el) + 0.5) * res
    K = int(math.ceil(tol_deg / res)) + 1
    rs = np.floor((el - el_min) / res).astype(np.int64)
    off = np.arange(-K, K + 1)
    rr = rs[:, None] + off[None, :]
    valid = (rr >= 0) & (rr < n_el)
    rr = np.clip(rr, 0, n_el - 1)
    e_s = np.radians(el)[:, None]
    e_r = np.radians(row_el[rr])
    cosd = (math.cos(math.radians(tol_deg)) - np.sin(e_s) * np.sin(e_r)) / (np.cos(e_s) * np.cos(e_r) + 1e-12)
    valid &= cosd < 1.0
    full = cosd <= -1.0
    daz = np.degrees(np.arccos(np.clip(cosd, -1.0, 1.0)))
    a0 = (az[:, None] - daz)
    a1 = (az[:, None] + daz)
    diff = np.zeros((n_el, n_az + 1), np.int32)
    rr_v = rr[valid]
    full_v = full[valid]
    c0 = np.floor(a0[valid] / res).astype(np.int64)
    c1 = np.floor(a1[valid] / res).astype(np.int64)
    # full rows
    np.add.at(diff, (rr_v[full_v], np.zeros(int(full_v.sum()), np.int64)), 1)
    np.add.at(diff, (rr_v[full_v], np.full(int(full_v.sum()), n_az, np.int64)), -1)
    part = ~full_v
    rr_p, c0, c1 = rr_v[part], c0[part], c1[part]
    span = np.minimum(c1 - c0 + 1, n_az)
    s0 = np.mod(c0, n_az)
    e0 = s0 + span                      # exclusive end, may exceed n_az (wrap)
    no_wrap = e0 <= n_az
    np.add.at(diff, (rr_p[no_wrap], s0[no_wrap]), 1)
    np.add.at(diff, (rr_p[no_wrap], e0[no_wrap]), -1)
    w = ~no_wrap
    np.add.at(diff, (rr_p[w], s0[w]), 1)
    np.add.at(diff, (rr_p[w], np.full(int(w.sum()), n_az, np.int64)), -1)
    np.add.at(diff, (rr_p[w], np.zeros(int(w.sum()), np.int64)), 1)
    np.add.at(diff, (rr_p[w], e0[w] - n_az), -1)
    return np.cumsum(diff, axis=1)[:, :n_az] > 0


def _attitude_matrices(yaw_deg: np.ndarray, pitch_deg: np.ndarray, roll_deg: np.ndarray) -> np.ndarray:
    """Vectorised :func:`attitude_matrix` -> (N, 3, 3)."""
    y, p, r = np.radians(yaw_deg), np.radians(pitch_deg), np.radians(roll_deg)
    cy, sy, cp, sp_, cr, sr = np.cos(y), np.sin(y), np.cos(p), np.sin(p), np.cos(r), np.sin(r)
    fwd = np.stack([cp * sy, cp * cy, sp_], axis=-1)
    right0 = np.stack([cy, -sy, np.zeros_like(y)], axis=-1)
    down0 = np.cross(fwd, right0)
    right = cr[:, None] * right0 + sr[:, None] * down0
    down = cr[:, None] * down0 - sr[:, None] * right0
    return np.stack([right, down, fwd], axis=1)


def _grid(center: float, half: float, step: float) -> np.ndarray:
    n = max(0, int(math.ceil(half / step - 1e-9)))
    return center + step * np.arange(-n, n + 1)


def _fov_solid_angle(intr: Intrinsics, w: int, h: int) -> float:
    a = 2 * math.atan((w / 2.0) / intr.fx)
    b = 2 * math.atan((h / 2.0) / intr.fy)
    return 4.0 * math.asin(math.sin(a / 2) * math.sin(b / 2))


@dataclass
class _Candidate:
    score: float
    M: np.ndarray
    intr: Intrinsics


@dataclass
class _Fit:
    M: np.ndarray
    intr: Intrinsics
    det_idx: np.ndarray
    cat_idx: np.ndarray
    resid_px: np.ndarray
    rms_px: float
    rms_deg: float
    sigma_deg: float
    cov: Optional[np.ndarray] = None


# --- coarse search ---------------------------------------------------------

def _coarse_search(det: Detections, cat_az, cat_el, cat_mag, prior: Prior, intr0: Intrinsics,
                   sp: SolveParams, fit_intr: bool, step: float = 1.0,
                   n_cands: Optional[int] = None) -> Tuple[List[_Candidate], dict]:
    W, H = prior.width, prior.height
    n_c = min(sp.coarse_detections, len(det))
    uv = det.uv[:n_c]
    # catalog subset: about 2.5x as many stars per field as detections used
    omega = _fov_solid_angle(intr0, W, H)
    n_target = int(2.5 * n_c * (2.0 * math.pi) / max(omega, 1e-3))
    order = np.argsort(cat_mag)[:max(n_target, 3 * n_c)]
    res = sp.coarse_res_deg
    el_min = -2.0
    # tolerance per field-angle zone: grid quantisation (pitch/roll 1 deg -> up
    # to 0.5 deg, bitmap cells 0.2 deg) + focal length/k1 grid steps x angle
    tols = (0.65, 0.9, 1.3, 2.0)
    if prior.pitch_deg is None or prior.roll_deg is None:
        # first pass of a blind search: 2-deg pitch/roll steps, so up to 1 deg more
        # error; it only ranks hypotheses for the fine pass that follows
        tols = tuple(t + 0.7 for t in tols)
    bitmaps = np.stack([_sky_bitmap(cat_az[order], cat_el[order], t, res, el_min) for t in tols])
    n_az = bitmaps.shape[2]
    # tolerance zone per detection by its field angle
    r_norm = np.hypot((uv[:, 0] - intr0.cx) / intr0.fx, (uv[:, 1] - intr0.cy) / intr0.fy)
    theta = np.degrees(np.arctan(r_norm))
    zone = np.digitize(theta, (10.0, 22.0, 35.0))
    # expected random score (fraction of sky cells set, per zone)
    frac = bitmaps[:, int((30 - el_min) / res):, :].mean(axis=(1, 2))
    random_score = float(frac[zone].sum())

    # search grids
    # ``step``: pitch/roll grid step around a prior
    def angle_grid(which, full_lo, full_hi, full_step):
        v = getattr(prior, which + "_deg")
        if v is None:
            return np.arange(full_lo, full_hi, full_step), True
        s = prior.sigma(which)
        return _grid(v, max(2.5 * s, step), step), False

    pitches, pitch_unknown = angle_grid("pitch", -10.0, 90.0 + 1e-9, 2.0)
    pitches = np.unique(np.clip(pitches, -89.0, 90.0))
    rolls, roll_unknown = angle_grid("roll", -180.0, 180.0, 2.0 if pitch_unknown else step)
    if roll_unknown and prior.yaw_deg is None and pitches.min() > 80.0:
        rolls = np.array([0.0])  # at the zenith yaw already covers rotation about the axis
    if fit_intr:
        sf = prior.fov_sigma_frac
        fscale = np.exp(_grid(0.0, 2.0 * sf, 0.025)) if sf > 0 else np.array([1.0])
        rc = math.hypot(W / 2.0, H / 2.0) / intr0.fx
        if prior.k1 is not None:
            k1s = np.array([prior.k1])
        elif rc ** 2 * (prior.k1_range[1] - prior.k1_range[0]) < 0.03:
            k1s = np.array([0.5 * (prior.k1_range[0] + prior.k1_range[1])])
        else:
            kstep = 0.05 / rc ** 2
            k1s = np.arange(prior.k1_range[0], prior.k1_range[1] + 1e-9, kstep)
    else:
        fscale = np.array([1.0])
        k1s = np.array([intr0.k1])

    # yaw shifts (bitmap columns)
    if prior.yaw_deg is None:
        shifts = np.arange(n_az)
    else:
        s = prior.sigma("yaw")
        half = max(3.0 * s, 3.0)
        shifts = np.unique(np.mod(np.round((prior.yaw_deg + np.arange(-half, half + res, res)) / res).astype(np.int64), n_az))

    n_combo = len(pitches) * len(rolls) * len(fscale) * len(k1s)
    # keep the work bounded (the fully-unknown case): thin f/k1 first
    budget = sp.coarse_budget
    thinned = False
    while n_combo * n_c * len(shifts) > budget and (len(fscale) > 1 or len(k1s) > 1):
        if len(k1s) >= len(fscale):
            k1s = k1s[::2]
        else:
            fscale = fscale[len(fscale) // 2::max(1, len(fscale))] if len(fscale) <= 2 else fscale[::2]
        n_combo = len(pitches) * len(rolls) * len(fscale) * len(k1s)
        thinned = True

    # Flat lookup table: per zone, n_el rows + one all-false row (below the
    # horizon/outside), each row doubled in azimuth so that az + yaw needs no modulo.
    n_el = bitmaps.shape[1]
    table = np.zeros((len(tols), n_el + 1, 2 * n_az), np.uint8)
    table[:, :n_el, :n_az] = bitmaps
    table[:, :n_el, n_az:] = bitmaps
    table = table.ravel()
    pp, rr = np.meshgrid(pitches.astype(float), rolls.astype(float), indexing="ij")
    pp, rr = pp.ravel(), rr.ravel()
    Ms = _attitude_matrices(np.zeros_like(pp), pp, rr)                # (n_pr, 3, 3)
    shifts = shifts.astype(np.intp)
    chunk = max(1, int(4e6 // (n_c * len(shifts))))
    keep = 4
    rec_score, rec_yaw, rec_pr, rec_fk = [], [], [], []
    fks = [(float(fs), float(k1)) for fs in fscale for k1 in k1s]
    for fki, (fs, k1) in enumerate(fks):
        intr = intr0.with_(fx=intr0.fx * fs, fy=intr0.fy * fs, k1=k1, k2=intr0.k2 if not fit_intr else 0.0)
        rays = unproject(uv, intr)
        enu = np.einsum("dk,pkj->pdj", rays, Ms)                        # (n_pr, n_c, 3): rays @ M
        az0 = np.degrees(np.arctan2(enu[..., 0], enu[..., 1])) % 360.0
        el = np.degrees(np.arcsin(np.clip(enu[..., 2], -1, 1)))
        ei = np.floor((el - el_min) / res).astype(np.intp)
        ei = np.where((ei >= 0) & (ei < n_el), ei, n_el)
        ai = np.minimum(np.floor(az0 / res).astype(np.intp), n_az - 1)
        base = (zone[None, :] * (n_el + 1) + ei) * (2 * n_az) + ai      # (n_pr, n_c)
        for c0 in range(0, len(pp), chunk):
            bb = base[c0:c0 + chunk]
            score = table[bb[:, :, None] + shifts[None, None, :]].sum(axis=1, dtype=np.int32)  # (n, n_yaw)
            k = min(keep, score.shape[1])
            top = np.argpartition(-score, k - 1, axis=1)[:, :k]
            rows = np.arange(bb.shape[0])[:, None]
            rec_score.append(score[rows, top].ravel())
            rec_yaw.append(shifts[top].ravel())
            rec_pr.append(np.repeat(np.arange(c0, c0 + bb.shape[0]), k))
            rec_fk.append(np.full(bb.shape[0] * k, fki))
    sc_all = np.concatenate(rec_score)
    yaw_all = np.concatenate(rec_yaw)
    pr_all = np.concatenate(rec_pr)
    fk_all = np.concatenate(rec_fk)
    n_top = min(len(sc_all), 400)
    order_top = np.argpartition(-sc_all, n_top - 1)[:n_top]
    order_top = order_top[np.argsort(-sc_all[order_top], kind="stable")]
    cands: List[_Candidate] = []
    for i in order_top:
        M = attitude_matrix(float(yaw_all[i] * res), float(pp[pr_all[i]]), float(rr[pr_all[i]]))
        if any(np.degrees(math.acos(min(1.0, (np.trace(c.M @ M.T) - 1) / 2))) < 3.0 for c in cands):
            continue
        fs, k1 = fks[fk_all[i]]
        intr = intr0.with_(fx=intr0.fx * fs, fy=intr0.fy * fs, k1=k1, k2=intr0.k2 if not fit_intr else 0.0)
        cands.append(_Candidate(float(sc_all[i]), M, intr))
        if len(cands) >= (n_cands or sp.coarse_candidates):
            break
    info = {"coarse_wide": bool(thinned or pitch_unknown or roll_unknown or len(pitches) * len(rolls) > 400),"coarse_best": cands[0].score if cands else 0, "coarse_second": cands[1].score if len(cands) > 1 else 0,
            "coarse_random": round(random_score, 2), "coarse_used": n_c, "coarse_combos": n_combo,
            "coarse_yaws": int(len(shifts))}
    return cands, info


# --- refinement ------------------------------------------------------------

_PNAMES = ("wx", "wy", "wz", "logf", "aspect", "dcx", "dcy", "k1", "k2")


def _apply(p: np.ndarray, M0: np.ndarray, intr0: Intrinsics) -> Tuple[np.ndarray, Intrinsics]:
    M = rotvec_matrix(p[0:3]) @ M0
    f = intr0.fx * math.exp(p[3])
    intr = Intrinsics(f, f * (1.0 + p[4]), intr0.cx + p[5], intr0.cy + p[6], p[7], p[8])
    return M, intr


def _match(uv_det: np.ndarray, flux: np.ndarray, sat: np.ndarray, cat_enu: np.ndarray, cat_mag: np.ndarray,
           cat_blend: np.ndarray, M: np.ndarray, intr: Intrinsics, W: int, H: int, radius: float,
           photometric: bool) -> Tuple[np.ndarray, np.ndarray]:
    """Nearest-neighbour detection -> catalog matches (indices into the inputs)."""
    cam = cat_enu @ M.T
    uv, ok = project_cam(cam, intr)
    ok &= (uv[:, 0] > -radius) & (uv[:, 0] < W + radius) & (uv[:, 1] > -radius) & (uv[:, 1] < H + radius)
    ci = np.nonzero(ok)[0]
    if ci.size == 0 or uv_det.shape[0] == 0:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    cuv = uv[ci].astype(np.float32)
    du = uv_det[:, None, 0].astype(np.float32) - cuv[None, :, 0]
    dv = uv_det[:, None, 1].astype(np.float32) - cuv[None, :, 1]
    d = du * du + dv * dv
    if d.shape[1] > 1:
        part = np.partition(d, 1, axis=1)
        d1, d2 = part[:, 0], part[:, 1]
    else:
        d1, d2 = d[:, 0], np.full(d.shape[0], np.inf, np.float32)
    j = np.argmin(d, axis=1)
    dmin = np.sqrt(d1)
    # ratio test: the second-nearest catalog star must be clearly farther;
    # blended catalog stars (close pairs) are not used
    good = (dmin < radius) & ~cat_blend[ci[j]] & (np.sqrt(d2) > 1.5 * dmin + 0.5)
    di = np.nonzero(good)[0]
    cj = j[di]
    # one-to-one: keep the closest detection per catalog star
    o = np.argsort(dmin[di])
    di, cj = di[o], cj[o]
    _, first = np.unique(cj, return_index=True)
    di, cj = di[first], cj[first]
    if photometric and di.size >= 10:
        inst = -2.5 * np.log10(np.maximum(flux[di], 1e-6))
        cm = cat_mag[ci[cj]]
        uns = ~sat[di]
        if uns.sum() >= 5:
            zp = np.median((cm - inst)[uns])
            # detection much brighter than the catalog star predicts -> wrong match
            # (thin cloud only ever makes stars fainter, so no lower bound)
            keep = ~(uns & ((cm - (inst + zp)) > 1.5))
            di, cj = di[keep], cj[keep]
    return di, ci[cj]


def _residuals(p, M0, intr0, cat_enu, uv_det):
    M, intr = _apply(p, M0, intr0)
    uv, ok = project_cam(cat_enu @ M.T, intr, r2_max=1e6)
    return (uv - uv_det).ravel(), ok


def _lm(p0, free, M0, intr0, cat_enu, uv_det, prior_sig, prior_mu, iters=12):
    """Robust LM on pixel residuals; returns params, inlier mask, covariance (free)."""
    p = p0.copy()
    fi = np.nonzero(free)[0]
    steps = np.array([1e-6, 1e-6, 1e-6, 1e-6, 1e-6, 1e-3, 1e-3, 1e-6, 1e-6])
    lam = 1e-3
    n = uv_det.shape[0]
    inl = np.ones(n, bool)

    def cost_terms(pp, wts, sig):
        r, _ = _residuals(pp, M0, intr0, cat_enu, uv_det)
        rw = r * np.repeat(np.sqrt(wts), 2) / sig
        pr = (pp[fi] - prior_mu[fi]) / prior_sig[fi]
        return np.concatenate([rw, pr[np.isfinite(prior_sig[fi])]])

    sig = 1.0
    wts = np.ones(n)
    for it in range(iters):
        r, _ = _residuals(p, M0, intr0, cat_enu, uv_det)
        rr = np.hypot(r[0::2], r[1::2])
        sig = max(1.4826 * float(np.median(rr[inl])) / 1.1774 if inl.any() else 1.0, 0.05)  # per-axis sigma
        k = 1.5 * sig
        wts = np.where(rr <= k, 1.0, k / np.maximum(rr, 1e-12))
        inl = rr < max(3.5 * sig * 1.1774, 0.3)
        wts = wts * inl
        f0 = cost_terms(p, wts, sig)
        J = np.empty((f0.size, fi.size))
        for a, idx in enumerate(fi):
            q = p.copy()
            q[idx] += steps[idx]
            J[:, a] = (cost_terms(q, wts, sig) - f0) / steps[idx]
        JTJ = J.T @ J
        g = J.T @ f0
        c0 = float(f0 @ f0)
        dp = np.zeros(fi.size)
        for _ in range(8):
            try:
                dp = -np.linalg.solve(JTJ + lam * np.diag(np.diag(JTJ) + 1e-12), g)
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            q = p.copy()
            q[fi] += dp
            c1 = float(np.sum(cost_terms(q, wts, sig) ** 2))
            if c1 < c0:
                p = q
                lam = max(lam / 3, 1e-7)
                break
            lam *= 5
        else:
            break
        if it > 2 and np.all(np.abs(dp) < steps[fi]):
            break
    try:
        cov = np.linalg.inv(JTJ)
    except np.linalg.LinAlgError:
        cov = None
    return p, inl, cov, sig


def _refine(cand: _Candidate, det: Detections, n_use: int, cat_enu, cat_mag, cat_blend, prior: Prior,
            intr_nom: Intrinsics, sp: SolveParams, fit_intr: bool, deg_px: float,
            ref_counts: Optional[List[int]] = None) -> Tuple[Optional[_Fit], List[int]]:
    """Refine one coarse candidate. With ``ref_counts`` (per-stage match counts of
    the best solution so far) a candidate falling far behind is abandoned early."""
    W, H = prior.width, prior.height
    uv_all = det.uv[:n_use]
    flux, sat = det.flux[:n_use], det.saturated[:n_use]
    M0 = cand.M
    intr0 = cand.intr.with_(cx=intr_nom.cx, cy=intr_nom.cy)
    p = np.zeros(9)
    p[7], p[8] = intr0.k1, intr0.k2
    intr0 = intr0.with_(k1=0.0, k2=0.0)
    nan = np.nan
    # priors (sigma) on parameters; nan = none
    prior_sig = np.array([nan, nan, nan, nan, 0.003, 0.01 * W, 0.01 * W, 0.5, 0.15])
    prior_mu = np.zeros(9)
    prior_mu[7] = prior.k1 if prior.k1 is not None else 0.0
    prior_mu[8] = prior.k2 if prior.k2 is not None else 0.0
    if prior.k1 is not None:
        prior_sig[7] = 0.05
    base_free = np.array([1, 1, 1, 0, 0, 0, 0, 0, 0], bool)
    r0 = max(12.0, 1.0 / deg_px)
    n_c = min(sp.coarse_detections, n_use)
    # (radius px, detections used, catalog mag limit offset, free params, photometric)
    stages = [
        (r0, 2 * n_c, 4.5, "rf", False),
        (0.6 * r0, 4 * n_c, 5.5, "rfk", False),
        (max(6.0, 0.3 * r0), n_use, 99, "rfkc", True),
        (4.0, n_use, 99, "all", True),
        (sp.final_radius_px, n_use, 99, "all", True),
    ]
    fit = None
    order_mag = cat_mag
    k2_ok = False
    counts: List[int] = []
    for si, (rad, ndet, mlim, what, photo) in enumerate(stages):
        free = base_free.copy()
        if fit_intr:
            if "f" in what or what == "all":
                free[3] = True
            if "k" in what or what == "all":
                free[7] = True
            if "c" in what or what == "all":
                free[5:7] = True
            if what == "all":
                free[4] = True
                free[8] = k2_ok
        sel_cat = order_mag <= (np.sort(order_mag)[min(len(order_mag) - 1, int(5 * ndet))] if mlim < 50 else 99)
        cat_idx_pool = np.nonzero(sel_cat)[0]
        for rep in range(3):
            M, intr = _apply(p, M0, intr0)
            di, cj = _match(uv_all[:ndet], flux[:ndet], sat[:ndet], cat_enu[cat_idx_pool], cat_mag[cat_idx_pool],
                            cat_blend[cat_idx_pool], M, intr, W, H, rad, photo)
            cj = cat_idx_pool[cj]
            if di.size < 8:
                return None, counts
            nfree = int(free.sum())
            if di.size < 2 * nfree:
                free = base_free.copy()
            p, inl, cov, sig = _lm(p, free, M0, intr0, cat_enu[cj], uv_all[di], prior_sig, prior_mu)
            if inl.sum() < 8:
                return None, counts
        counts.append(int(inl.sum()))
        if ref_counts is not None and len(ref_counts) > si and counts[-1] < 0.5 * ref_counts[si]:
            return None, counts
        k2_ok = bool(fit_intr and di.size >= sp.fit_k2_min_stars)
        fit = (p.copy(), di[inl], cj[inl], cov, free.copy(), sig)
    p, di, cj, cov, free, sig = fit
    M, intr = _apply(p, M0, intr0)
    uv_pred, _ = project_cam(cat_enu[cj] @ M.T, intr, r2_max=1e6)
    res = uv_pred - uv_all[di]
    rms_px = float(np.sqrt(np.mean(np.sum(res ** 2, axis=1))))
    rays = unproject(uv_all[di], intr) @ M
    cosang = np.clip(np.sum(rays * cat_enu[cj], axis=1), -1, 1)
    rms_deg = float(np.degrees(np.sqrt(np.mean(np.arccos(cosang) ** 2))))
    sigma_deg = float("inf")
    if cov is not None:
        fi = np.nonzero(free)[0]
        rot = [i for i, k in enumerate(fi) if k < 3]
        if len(rot) == 3:
            c = cov[np.ix_(rot, rot)]
            # 1-sigma of the total rotation angle error (all three axes)
            sigma_deg = float(np.degrees(np.sqrt(max(float(np.trace(c)), 0.0))))
    return _Fit(M, intr, di, cj, res, rms_px, rms_deg, sigma_deg, cov), counts


def _blended(enu: np.ndarray, mag: np.ndarray, sep_deg: float, dmag: float = 2.5) -> np.ndarray:
    """Catalog stars with a neighbour closer than ``sep_deg`` that is less than
    ``dmag`` fainter: their image is a blend, so they are not used."""
    out = np.zeros(len(mag), bool)
    cmin = math.cos(math.radians(sep_deg))
    step = 512
    for i in range(0, len(mag), step):
        dots = enu[i:i + step] @ enu.T
        close = dots > cmin
        close[np.arange(close.shape[0]), np.arange(i, i + close.shape[0])] = False
        fainter_by = mag[None, :] - mag[i:i + step, None]
        out[i:i + step] = np.any(close & (fainter_by < dmag), axis=1)
    return out


def _coverage(uv: np.ndarray, M: np.ndarray, intr: Intrinsics, W: int, H: int, n: int = 4) -> float:
    """Fraction of the sky cells (n x n grid, cell centre above 2 deg) holding a matched star."""
    gx = (np.arange(n) + 0.5) * W / n
    gy = (np.arange(n) + 0.5) * H / n
    cu, cv = np.meshgrid(gx, gy)
    centres = np.stack([cu.ravel(), cv.ravel()], axis=1)
    up = (unproject(centres, intr) @ M)[:, 2]
    sky = up > math.sin(math.radians(2.0))
    if not sky.any():
        return 0.0
    ix = np.clip((uv[:, 0] * n / W).astype(int), 0, n - 1)
    iy = np.clip((uv[:, 1] * n / H).astype(int), 0, n - 1)
    occ = np.zeros(n * n, bool)
    occ[iy * n + ix] = True
    return float((occ & sky).sum() / sky.sum())


# --- entry point -----------------------------------------------------------

def calibrate(image: np.ndarray, t_ns: int, observer: Observer, prior: Prior,
              params: Optional[SolveParams] = None, catalog: Optional[StarCatalog] = None) -> StarCalResult:
    """Solve the camera attitude (and intrinsics) from a stacked night-sky frame.

    ``t_ns``: UTC time of the middle of the stack, ns since the Unix epoch.
    Never returns ``ok=True`` unless all acceptance checks pass; ``reason``
    explains a failure.
    """
    t0 = time.monotonic()
    sp = params or SolveParams()
    img = to_gray_float(image)
    sun_alt = sun_altitude_deg(t_ns, observer)
    moon_az, moon_alt = astro.moon_altaz(t_ns, observer.lat, observer.lon, observer.alt_m)
    common = dict(sun_alt_deg=round(sun_alt, 3), moon_alt_deg=round(moon_alt, 2))
    if img.shape != (prior.height, prior.width):
        return _fail(t_ns, f"image is {img.shape[1]}x{img.shape[0]}, prior says {prior.width}x{prior.height}", t0, **common)
    if sun_alt > sp.max_sun_alt_deg:
        return _fail(t_ns, f"sun too high ({sun_alt:.1f} deg > {sp.max_sun_alt_deg:.0f} deg)", t0, **common)

    det = detect_stars(img, sp.detect)
    common["stars_detected"] = len(det)
    if len(det) < sp.min_stars:
        return _fail(t_ns, f"too few stars detected ({len(det)} < {sp.min_stars}): cloud, glare or daylight", t0,
                     **common)

    cat = catalog or load_catalog()
    az, el = astro.stars_altaz(cat.ra_deg, cat.dec_deg, cat.pmra_masyr, cat.pmdec_masyr, t_ns,
                               observer.lat, observer.lon, observer.pressure, observer.temperature_c, observer.dut1_s)
    vis = el > sp.min_el_deg
    if moon_alt > -1.0:
        # stars inside the Moon's glare are not used (positions to ~0.3 deg)
        ma = astro.azel_to_enu(moon_az, moon_alt)
        vis &= astro.azel_to_enu(az, el) @ ma < math.cos(math.radians(3.0))
    cat_az, cat_el, cat_mag = az[vis], el[vis], cat.vmag[vis]
    cat_enu = astro.azel_to_enu(cat_az, cat_el)

    W, H = prior.width, prior.height
    intr_nom = Intrinsics.from_fov(W, H, prior.fov_h_deg)
    if prior.fx is not None:
        intr_nom = Intrinsics(prior.fx, prior.fy or prior.fx,
                              prior.cx if prior.cx is not None else intr_nom.cx,
                              prior.cy if prior.cy is not None else intr_nom.cy,
                              prior.k1 or 0.0, prior.k2 or 0.0)
    fit_intr = prior.fit_intrinsics
    deg_px = math.degrees(math.atan(1.0 / intr_nom.fx))
    cat_blend = _blended(cat_enu, cat_mag, 3.0 * deg_px)

    cands, info = _coarse_search(det, cat_az, cat_el, cat_mag, prior, intr_nom, sp, fit_intr)
    if info["coarse_wide"] and cands:
        # Wide or unknown prior: the first grid was coarse (or thinned in focal
        # length/k1). Re-search finely around each of its best hypotheses.
        cands0, cands = _coarse_search(det, cat_az, cat_el, cat_mag, prior, intr_nom, sp, fit_intr,
                                       n_cands=sp.wide_candidates)[0], []
        for c in cands0:
            y, p, r = matrix_to_attitude(c.M)
            local = replace(prior, yaw_deg=y, pitch_deg=min(p, 90.0), roll_deg=r, sigma_deg=0.8, yaw_sigma_deg=None,
                            pitch_sigma_deg=None, roll_sigma_deg=None)
            cands += _coarse_search(det, cat_az, cat_el, cat_mag, local, intr_nom, sp, fit_intr, step=0.5,
                                    n_cands=2)[0]
        cands.sort(key=lambda c: -c.score)
        uniq: List[_Candidate] = []
        for c in cands:
            if all(math.degrees(math.acos(min(1.0, (np.trace(u.M @ c.M.T) - 1) / 2))) >= 3.0 for u in uniq):
                uniq.append(c)
        cands = uniq[:sp.coarse_candidates]
        info["coarse_best"] = cands[0].score if cands else 0
    checks = dict(info)
    # The coarse score only ranks hypotheses; the strict checks come after refinement.
    if not cands or cands[0].score < max(8.0, info["coarse_random"] + 6.0):
        return _fail(t_ns, f"no plausible star pattern found (best coarse score {info['coarse_best']:.0f} of "
                           f"{info['coarse_used']}, chance level {info['coarse_random']:.1f})", t0, checks=checks,
                     **common)

    n_use = min(len(det), sp.max_detections)
    fits: List[_Fit] = []
    ref: Optional[List[int]] = None
    for c in cands:
        if time.monotonic() - t0 > sp.time_budget_s:
            break
        f, counts = _refine(c, det, n_use, cat_enu, cat_mag, cat_blend, prior, intr_nom, sp, fit_intr, deg_px, ref)
        if f is not None:
            fits.append(f)
            if ref is None or len(f.det_idx) > len(fits[0].det_idx):
                ref = counts
    if not fits:
        return _fail(t_ns, "no candidate attitude survived refinement", t0, checks=checks, **common)
    fits.sort(key=lambda f: (-len(f.det_idx), f.rms_px))
    best = fits[0]
    yaw, pitch, roll = matrix_to_attitude(best.M, prior.yaw_deg or 0.0)
    bi = best.intr
    out = dict(yaw_deg=float(yaw), pitch_deg=float(pitch), roll_deg=float(roll), fx=float(bi.fx), fy=float(bi.fy),
               cx=float(bi.cx), cy=float(bi.cy), k1=float(bi.k1), k2=float(bi.k2),
               stars_matched=int(len(best.det_idx)), rms_deg=float(best.rms_deg), rms_px=float(best.rms_px),
               sigma_deg=float(best.sigma_deg))
    uv_m = det.uv[best.det_idx]
    cov_frac = _coverage(uv_m, best.M, best.intr, W, H)
    out["coverage"] = float(cov_frac)

    # acceptance checks
    n_b = min(30, len(det))
    bright_matched = np.isin(np.arange(n_b), best.det_idx).mean()
    # chance matches: detection density x final match area x catalog stars in frame
    area_frac = math.pi * sp.final_radius_px ** 2 / float(W * H)
    n_cat_frame = int(np.sum(project_cam(cat_enu @ best.M.T, best.intr)[1]))
    expected_chance = n_use * area_frac * n_cat_frame
    checks.update(bright_matched_frac=round(float(bright_matched), 3), chance_matches=round(expected_chance, 2),
                  catalog_in_frame=n_cat_frame, candidates_refined=len(fits))
    reasons = []
    if out["stars_matched"] < sp.min_stars:
        reasons.append(f"only {out['stars_matched']} stars matched (< {sp.min_stars})")
    if best.rms_deg > sp.max_rms_deg or best.rms_px > sp.max_rms_px:
        reasons.append(f"fit residual too large ({best.rms_deg:.3f} deg, {best.rms_px:.2f} px)")
    if best.sigma_deg > sp.max_sigma_deg:
        reasons.append(f"attitude poorly determined (sigma {best.sigma_deg:.3f} deg)")
    if fit_intr and cov_frac < sp.min_coverage:
        reasons.append(f"matched stars cover only {cov_frac:.0%} of the sky in the frame")
    if bright_matched < sp.min_bright_matched_frac:
        reasons.append(f"only {bright_matched:.0%} of the brightest sources match catalog stars")
    if out["stars_matched"] < 5 * expected_chance + 10:
        reasons.append("matches not significant against chance")
    if fit_intr:
        f_ratio = best.intr.fx / intr_nom.fx
        if abs(math.log(f_ratio)) > 3 * prior.fov_sigma_frac + 0.03:
            reasons.append(f"focal length {f_ratio:.3f}x nominal is implausible")
        if abs(best.intr.cx - intr_nom.cx) > 0.05 * W or abs(best.intr.cy - intr_nom.cy) > 0.05 * W:
            reasons.append("principal point implausible")
    # ambiguity: another distinct attitude that fits nearly as well
    for f in fits[1:]:
        ang = math.degrees(math.acos(min(1.0, (np.trace(f.M @ best.M.T) - 1) / 2)))
        if ang > 1.0 and len(f.det_idx) >= 0.7 * len(best.det_idx) and f.rms_px <= 1.5 * sp.max_rms_px:
            reasons.append(f"ambiguous: a second attitude {ang:.1f} deg away also fits ({len(f.det_idx)} stars)")
            break
    if fit_intr and prior.yaw_deg is not None:
        dyaw = (yaw - prior.yaw_deg + 180.0) % 360.0 - 180.0
        checks["yaw_minus_prior_deg"] = round(dyaw, 3)
    if reasons:
        # Never hand out numbers from a rejected solution as if they were usable;
        # they stay available for diagnostics only.
        checks["rejected_solution"] = {k: (round(v, 5) if isinstance(v, float) else v) for k, v in out.items()}
        res = StarCalResult(ok=False, reason="; ".join(reasons), t_ns=t_ns, checks=checks,
                            stars_matched=out["stars_matched"], **common)
    else:
        res = StarCalResult(ok=True, reason="ok", t_ns=t_ns, checks=checks, **out, **common)
    res.elapsed_s = time.monotonic() - t0
    return res
