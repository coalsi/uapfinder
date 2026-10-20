"""Star detection on a stacked night-sky frame.

Steps: grey float image -> hot-pixel cleaning (isolated pixels far above
their 3x3 median) -> smooth background (block medians, median-filtered and
interpolated; follows light-pollution gradients, cloud glow and Moon glare
on scales > ~2 blocks) -> local noise (block MAD) -> matched filter
(Gaussian) threshold -> connected components -> per-source windowed,
intensity-weighted sub-pixel centroid, second moments and flux.

Rejected: components larger than ``max_area`` (Moon disc, glare structures,
bright cloud edges) together with a guard zone around them, elongated
sources (trails, satellites, aircraft), sources much wider than the median
PSF (diffuse blobs), sources at the image border.

Pixel coordinates follow OpenCV: the centre of pixel (0, 0) is (0.0, 0.0).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np


@dataclass
class DetectParams:
    block: int = 16                # background block size, px
    nsigma: float = 5.0            # detection threshold on the matched-filtered image
    psf_sigma: float = 1.2         # matched filter sigma, px
    window: int = 3                # centroid half-window, px (7x7)
    max_area: int = 400            # larger connected components are extended objects
    guard_factor: float = 2.0      # exclusion radius around extended objects (x their size)
    max_elongation: float = 2.0
    max_width_factor: float = 1.7  # vs the median source width (stars share the PSF)
    min_snr: float = 6.0           # aperture flux / its noise
    border: int = 4
    max_sources: int = 1500


@dataclass
class Detections:
    u: np.ndarray
    v: np.ndarray
    flux: np.ndarray
    peak: np.ndarray
    width: np.ndarray           # RMS radius from second moments, px
    saturated: np.ndarray
    snr: np.ndarray
    noise: float = 0.0
    background: float = 0.0
    n_extended: int = 0
    n_rejected: int = 0
    extended_area_frac: float = 0.0
    extended: list = field(default_factory=list)   # (u, v, radius) of rejected extended objects

    def __len__(self) -> int:
        return int(self.u.size)

    @property
    def uv(self) -> np.ndarray:
        return np.stack([self.u, self.v], axis=1)


def to_gray_float(image: np.ndarray) -> np.ndarray:
    img = np.asarray(image)
    if img.ndim == 3:
        img = img.astype(np.float32).mean(axis=2)
    return np.ascontiguousarray(img, dtype=np.float32)


def _block_stats(img: np.ndarray, block: int):
    h, w = img.shape
    nby, nbx = max(1, h // block), max(1, w // block)
    hh, ww = nby * block, nbx * block
    # Use centred crop for block stats; edges are covered by interpolation.
    oy, ox = (h - hh) // 2, (w - ww) // 2
    b = img[oy:oy + hh, ox:ox + ww].reshape(nby, block, nbx, block).transpose(0, 2, 1, 3).reshape(nby, nbx, -1)
    # Subsample pixels for speed (every 2nd row/col inside the block) - medians are robust.
    b = b[:, :, ::2]
    med = np.median(b, axis=2)
    mad = np.median(np.abs(b - med[:, :, None]), axis=2) * 1.4826
    return med.astype(np.float32), mad.astype(np.float32), (oy, ox)


def _upsample(grid: np.ndarray, shape, block: int, offset) -> np.ndarray:
    """Bilinear interpolation of block-centre values to every pixel."""
    h, w = shape
    nby, nbx = grid.shape
    yc = offset[0] + (np.arange(nby) + 0.5) * block - 0.5
    xc = offset[1] + (np.arange(nbx) + 0.5) * block - 0.5
    ys = np.clip(np.interp(np.arange(h), yc, np.arange(nby)), 0, nby - 1)
    xs = np.clip(np.interp(np.arange(w), xc, np.arange(nbx)), 0, nbx - 1)
    mapx = np.broadcast_to(xs[None, :], (h, w)).astype(np.float32)
    mapy = np.broadcast_to(ys[:, None], (h, w)).astype(np.float32)
    return cv2.remap(grid.astype(np.float32), mapx, mapy, interpolation=cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_REPLICATE)


def detect_stars(image: np.ndarray, params: Optional[DetectParams] = None,
                 mask: Optional[np.ndarray] = None) -> Detections:
    """Detect point sources. ``mask`` (bool, True = usable) is optional."""
    p = params or DetectParams()
    img = to_gray_float(image)
    h, w = img.shape
    sat_level = float(img.max())
    if np.issubdtype(np.asarray(image).dtype, np.integer):
        sat_level = min(sat_level, float(np.iinfo(np.asarray(image).dtype).max))

    # Background and noise (block medians are insensitive to hot pixels and stars).
    bmed, bmad, off = _block_stats(img, p.block)
    # The noise varies slowly; smooth it. The background is kept at block
    # resolution so that it follows cloud edges and glare.
    bmad = cv2.medianBlur(bmad, 5) if min(bmad.shape) >= 5 else bmad
    bg = _upsample(bmed, (h, w), p.block, off)
    noise_map = _upsample(np.maximum(bmad, 1e-3), (h, w), p.block, off)

    # Hot pixels: far above their 3x3 median while no neighbour is lit (a
    # star's PSF always lights its neighbours; a sensor defect does not).
    med3 = cv2.medianBlur(img, 3)
    k = np.ones((3, 3), np.uint8)
    k[1, 1] = 0
    neigh_max = cv2.dilate(img, k)
    above = img - bg
    hot = ((img - med3) > 6.0 * noise_map) & ((neigh_max - bg) < 0.15 * above + 2.0 * noise_map)
    clean = np.where(hot, med3, img)
    sub = clean - bg

    # Matched filter; noise of the filtered image scales by 1/(2 sqrt(pi) sigma).
    filt = cv2.GaussianBlur(sub, (0, 0), p.psf_sigma)
    fnoise = noise_map / (2.0 * np.sqrt(np.pi) * p.psf_sigma)
    det = filt > p.nsigma * fnoise
    if mask is not None:
        det &= mask
    det[:p.border, :] = False
    det[-p.border:, :] = False
    det[:, :p.border] = False
    det[:, -p.border:] = False

    n, labels, stats, cents = cv2.connectedComponentsWithStats(det.astype(np.uint8), connectivity=8)
    if n <= 1:
        return Detections(*(np.zeros(0) for _ in range(7)), noise=float(np.median(bmad)),
                          background=float(np.median(bmed)))
    area = stats[1:, cv2.CC_STAT_AREA]
    big = area > p.max_area
    extended = []
    ext_mask = np.zeros((h, w), np.uint8)
    ext_px = 0
    for i in np.nonzero(big)[0]:
        x0, y0, bw, bh = stats[i + 1, :4]
        ext_px += int(area[i])
        comp = labels[y0:y0 + bh, x0:x0 + bw] == i + 1
        if not np.any(img[y0:y0 + bh, x0:x0 + bw][comp] >= 0.98 * sat_level):
            continue  # faint extended structure (cloud edge, glow): drop it, no guard zone
        # A saturated extended object (the Moon, a street light): guard its glare.
        rad = 0.5 * max(bw, bh)
        cx, cy = cents[i + 1]
        guard = int(rad * p.guard_factor + 10)
        cv2.circle(ext_mask, (int(round(cx)), int(round(cy))), int(rad + guard), 1, -1)
        extended.append((float(cx), float(cy), float(rad)))

    # Peak of each small component in the filtered image.
    small = np.nonzero(~big)[0] + 1
    if small.size == 0:
        return Detections(*(np.zeros(0) for _ in range(7)), noise=float(np.median(bmad)),
                          background=float(np.median(bmed)), n_extended=int(big.sum()),
                          extended_area_frac=ext_px / float(h * w), extended=extended)
    lab_flat = labels.ravel()
    filt_flat = filt.ravel()
    sel = np.isin(lab_flat, small) if small.size < n - 1 else lab_flat > 0
    idx = np.nonzero(sel)[0]
    lab = lab_flat[idx]
    order = np.lexsort((-filt_flat[idx], lab))
    lab_sorted = lab[order]
    first = np.ones(lab_sorted.size, bool)
    first[1:] = lab_sorted[1:] != lab_sorted[:-1]
    peaks = idx[order][first]
    py, px = np.divmod(peaks, w)

    r = p.window
    ok = (px >= r + 1) & (px < w - r - 1) & (py >= r + 1) & (py < h - r - 1)
    ok &= ext_mask[py, px] == 0
    px, py = px[ok], py[ok]
    if px.size == 0:
        return Detections(*(np.zeros(0) for _ in range(7)), noise=float(np.median(bmad)),
                          background=float(np.median(bmed)), n_extended=int(big.sum()),
                          extended_area_frac=ext_px / float(h * w), extended=extended)
    dy, dx = np.mgrid[-r:r + 1, -r:r + 1]
    # Two passes: integer window on the peak, then re-centred on the first estimate.
    cu, cv_ = px.astype(np.float64), py.astype(np.float64)
    for _ in range(2):
        iu = np.rint(cu).astype(np.int64)
        iv = np.rint(cv_).astype(np.int64)
        iu = np.clip(iu, r, w - r - 1)
        iv = np.clip(iv, r, h - r - 1)
        win = sub[iv[:, None, None] + dy[None], iu[:, None, None] + dx[None]].astype(np.float64)
        loc_noise = noise_map[iv, iu].astype(np.float64)
        wts = np.clip(win - 1.0 * loc_noise[:, None, None], 0.0, None)
        tot = wts.sum(axis=(1, 2)) + 1e-12
        cu = iu + (wts * dx).sum(axis=(1, 2)) / tot
        cv_ = iv + (wts * dy).sum(axis=(1, 2)) / tot
    ddx = dx[None] - (cu - iu)[:, None, None]
    ddy = dy[None] - (cv_ - iv)[:, None, None]
    mxx = (wts * ddx * ddx).sum(axis=(1, 2)) / tot
    myy = (wts * ddy * ddy).sum(axis=(1, 2)) / tot
    mxy = (wts * ddx * ddy).sum(axis=(1, 2)) / tot
    tr = mxx + myy
    det2 = np.sqrt(np.maximum((mxx - myy) ** 2 + 4 * mxy * mxy, 0.0))
    l1 = 0.5 * (tr + det2)
    l2 = np.maximum(0.5 * (tr - det2), 1e-6)
    elong = np.sqrt(l1 / l2)
    width = np.sqrt(np.maximum(tr, 0.0))
    # Unclipped aperture sum: unbiased even where the noise is large (glare);
    # a clipped sum would rank noise peaks in bright regions as bright stars.
    flux = win.sum(axis=(1, 2))
    peak = win.max(axis=(1, 2))
    snr = flux / (loc_noise * (2 * r + 1) + 1e-6)   # noise of a (2r+1)^2 aperture sum
    raw_peak = img[iv, iu]
    saturated = raw_peak >= 0.98 * sat_level

    good = np.isfinite(cu) & np.isfinite(cv_) & (elong <= p.max_elongation) & (snr >= p.min_snr)
    ref = good & ~saturated & (snr >= 3 * p.min_snr)
    if ref.sum() >= 5:
        # PSF width from compact high-SNR sources (lower quartile: stars are the
        # narrowest sources; cloud-edge and glare residues are broader)
        wref = float(np.percentile(width[ref], 25))
        good &= (width <= p.max_width_factor * wref) | saturated
    n_rej = int((~good).sum())
    o = np.argsort(-flux[good])[:p.max_sources]

    def g(a):
        return a[good][o]
    return Detections(g(cu), g(cv_), g(flux), g(peak), g(width), g(saturated), g(snr),
                      noise=float(np.median(bmad)), background=float(np.median(bmed)),
                      n_extended=int(big.sum()), n_rejected=n_rej,
                      extended_area_frac=ext_px / float(h * w), extended=extended)
