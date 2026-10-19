"""Moving-point detection against a rolling sky background.

Pipeline per frame (Y plane only):

1. ``diff = |Y - B|`` where B is an exponentially weighted rolling average
   of past frames (time constant ``background_tau_s``, default 2 s). That
   absorbs the daytime sky gradient, slowly drifting haze and the stars,
   which move only 0.0042 deg/s (one pixel every ~11 s at 0.047 deg/px).
   Pixels inside current detections update 10x slower so a target does not
   burn into the background and cancel itself.
2. Robust noise: per-tile sigma from the median of |diff| (for Gaussian
   noise median|x| = 0.6745 sigma), so the threshold adapts to a bright
   horizon, the sun's glare or a dark zenith.
3. Detection at reduced resolution without losing point targets: |diff| is
   *max-pooled* (not area-averaged) by ``decimation``, so a 1-2 px target
   keeps its full contrast while the labelling work drops 4x.
4. Hysteresis threshold: components of the low mask (``low_threshold_sigma``)
   are kept only if they contain a seed pixel above ``threshold_sigma``. A
   morphological *closing* joins fragments of diffuse objects; there is
   deliberately no opening, which would erase single-pixel targets.
5. Each component is measured at full resolution: intensity-weighted
   sub-pixel centroid, area, bbox, peak, mean, effective area, SNR and
   polarity (brighter or darker than the sky: aircraft and birds are often
   dark silhouettes in daylight, which is why |diff| is used).

Backends: ``cpu`` uses OpenCV primitives. ``cuda`` runs the per-pixel
stages (background, difference, pooling, thresholds) through CuPy; labelling
and the per-blob measurement stay on the CPU because they touch only the
small binary masks and ROIs. JetPack's bundled OpenCV is built without
CUDA, so ``cv2.cuda`` would need a custom OpenCV build; CuPy installs from
a wheel and is NumPy-compatible, which also lets the exact same backend
code be exercised in tests with NumPy standing in for CuPy. The CUDA path
is NOT hardware-tested.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from .config import DetectionConfig

log = logging.getLogger(__name__)

_MAD_TO_SIGMA = 1.0 / 0.6745  # median(|N(0, s)|) = 0.6745 s
_WEIGHT_FLOOR_SIGMA = 2.0  # centroid weights are |diff| minus this many sigma
_NOISE_SAMPLE_STEP = 4


@dataclass
class Blob:
    u: float  # sub-pixel centroid, distorted image coordinates (OpenCV convention)
    v: float
    area: int  # full-resolution pixels above the low threshold
    bbox: Tuple[int, int, int, int]  # x, y, w, h at full resolution
    peak: float  # peak |frame - background|, DN
    mean: float  # mean |diff| over the blob's pixels
    flux: float  # sum of centroid weights
    eff_area: float  # flux / weighted peak: footprint of a point source ~ PSF area
    snr: float  # peak / local noise sigma
    sigma: float  # local noise sigma, DN
    polarity: int  # +1 brighter than the sky, -1 darker
    relative_intensity: float  # peak relative to the frame's dynamic range, 0..1


@dataclass
class DetectionResult:
    blobs: List[Blob] = field(default_factory=list)
    sigma: float = 0.0
    fg_fraction: float = 0.0
    disturbed: bool = False


def interpolated_median_abs(tiles: np.ndarray, max_value: int = 63) -> np.ndarray:
    """Median of non-negative integer samples along the last axis, interpolated.

    |diff| takes only integer values, so a plain median moves in 1 DN steps
    (0, 1, 2, ...), far too coarse when the sky noise is 1-2 DN. Treating the
    data as grouped (value k stands for the interval [k-0.5, k+0.5], value 0
    for [0, 0.5] since it is a rounded absolute value) and interpolating
    inside the median group gives a continuous estimate.
    """
    lead = tiles.shape[:-1]
    flat = np.minimum(tiles.reshape(-1, tiles.shape[-1]), max_value).astype(np.int64)
    n_groups = flat.shape[0]
    nb = max_value + 1
    idx = (np.arange(n_groups)[:, None] * nb + flat).ravel()
    counts = np.bincount(idx, minlength=n_groups * nb).reshape(n_groups, nb).astype(np.float64)
    cum = np.cumsum(counts, axis=1)
    half = cum[:, -1] / 2.0
    k = np.argmax(cum >= half[:, None], axis=1)
    rows = np.arange(n_groups)
    below = np.where(k > 0, cum[rows, np.maximum(k - 1, 0)], 0.0)
    in_group = np.maximum(counts[rows, k], 1.0)
    lower = np.where(k == 0, 0.0, k - 0.5)
    width = np.where(k == 0, 0.5, 1.0)
    return (lower + width * (half - below) / in_group).reshape(lead)


class Detector(ABC):
    """Backend-independent detection logic; subclasses implement the per-pixel stages."""

    name = "base"

    def __init__(self, cfg: DetectionConfig, width: int, height: int, fps: float):
        self.cfg = cfg
        self.width = width
        self.height = height
        self.d = cfg.decimation
        self.hc = height - height % self.d
        self.wc = width - width % self.d
        self.dec_shape = (self.hc // self.d, self.wc // self.d)
        self.alpha = 1.0 - math.exp(-cfg.background_update_every / (fps * cfg.background_tau_s))
        self.frame_index = 0
        self.initialized = False
        self._frames_since_noise = cfg.noise_update_every  # estimate on the first difference frame
        self._sigma_tiles = np.ones((cfg.noise_tiles_y, cfg.noise_tiles_x), dtype=np.float32)
        self._dyn_range = 255.0
        self._mask_dec: Optional[np.ndarray] = None  # uint8 0/255 at decimated resolution
        self._kernel = (cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (cfg.close_kernel_px, cfg.close_kernel_px))
                        if cfg.close_kernel_px > 1 else None)

    # --- backend hooks ------------------------------------------------------

    @abstractmethod
    def _reset_background(self, y: np.ndarray) -> None: ...

    @abstractmethod
    def _absdiff(self, y: np.ndarray) -> Any: ...

    @abstractmethod
    def _maxpool(self, diff: Any) -> Any: ...

    @abstractmethod
    def _to_host(self, arr: Any) -> np.ndarray: ...

    @abstractmethod
    def _set_threshold_maps(self, low: np.ndarray, high: np.ndarray) -> None: ...

    @abstractmethod
    def _threshold(self, dec: Any) -> Tuple[np.ndarray, np.ndarray]:
        """Host (low uint8 0/255, seeds bool) masks at decimated resolution."""

    @abstractmethod
    def _bg_roi(self, y0: int, y1: int, x0: int, x1: int) -> np.ndarray: ...

    @abstractmethod
    def _update_background(self, y: np.ndarray, rois: Sequence[Tuple[int, int, int, int]]) -> None: ...

    # --- shared logic -------------------------------------------------------

    def set_sky_mask(self, mask: Optional[np.ndarray]) -> None:
        """Full-resolution bool mask (True = sky); None disables masking."""
        if mask is None:
            self._mask_dec = None
            return
        off = self.d // 2
        sub = mask[off:self.hc:self.d, off:self.wc:self.d]
        self._mask_dec = np.where(sub, 255, 0).astype(np.uint8)

    @property
    def sigma(self) -> float:
        return float(np.median(self._sigma_tiles))

    def detect(self, y: np.ndarray) -> DetectionResult:
        cfg = self.cfg
        self.frame_index += 1
        if not self.initialized:
            self._reset_background(y)
            self.initialized = True
            return DetectionResult(sigma=self.sigma)

        diff = self._absdiff(y)
        self._frames_since_noise += 1
        if self._frames_since_noise >= cfg.noise_update_every:
            self._update_noise(self._to_host(diff[::_NOISE_SAMPLE_STEP, ::_NOISE_SAMPLE_STEP]), y)
            self._frames_since_noise = 0

        low, seeds = self._threshold(self._maxpool(diff))
        if self._mask_dec is not None:
            low = cv2.bitwise_and(low, self._mask_dec)
            seeds &= self._mask_dec > 0
        fg_fraction = cv2.countNonZero(low) / float(low.size)
        if fg_fraction > cfg.max_foreground_fraction:
            # Exposure jump, camera knock or lights: re-learn the sky rather than
            # emitting thousands of blobs.
            log.debug("frame disturbed (%.1f%% foreground); background reset", 100 * fg_fraction)
            self._reset_background(y)
            self._frames_since_noise = cfg.noise_update_every  # re-estimate noise on the next frame
            return DetectionResult(sigma=self.sigma, fg_fraction=fg_fraction, disturbed=True)
        if self._kernel is not None:
            low = cv2.morphologyEx(low, cv2.MORPH_CLOSE, self._kernel)
            if self._mask_dec is not None:
                low = cv2.bitwise_and(low, self._mask_dec)
        blobs = self._measure(y, diff, low, seeds)

        if self.frame_index % cfg.background_update_every == 0:
            self._update_background(y, [self._expand(b.bbox, 2) for b in blobs])
        return DetectionResult(blobs, self.sigma, fg_fraction, False)

    def _update_noise(self, sample: np.ndarray, y: np.ndarray) -> None:
        ty, tx = self._sigma_tiles.shape
        hs, ws = (sample.shape[0] // ty) * ty, (sample.shape[1] // tx) * tx
        tiles = sample[:hs, :ws].reshape(ty, hs // ty, tx, ws // tx).transpose(0, 2, 1, 3).reshape(ty, tx, -1)
        sigma = interpolated_median_abs(tiles) * _MAD_TO_SIGMA
        # A floor keeps thresholds meaningful on saturated or perfectly flat
        # regions (min_threshold_dn applies on top of k * sigma).
        self._sigma_tiles = np.maximum(sigma, 0.3).astype(np.float32)
        lo, hi = np.percentile(y[::8, ::8], (0.1, 99.9))
        self._dyn_range = max(float(hi - lo), 1.0)
        low = self._threshold_map(self.cfg.low_threshold_sigma)
        high = self._threshold_map(self.cfg.threshold_sigma)
        self._set_threshold_maps(low, high)

    def _threshold_map(self, k: float) -> np.ndarray:
        thr = np.maximum(self._sigma_tiles * k, self.cfg.min_threshold_dn)
        full = cv2.resize(thr, (self.dec_shape[1], self.dec_shape[0]), interpolation=cv2.INTER_LINEAR)
        # diff is integer, so diff > thr  <=>  diff > floor(thr).
        return np.clip(np.floor(full), 0, 254).astype(np.uint8)

    def _local_sigma(self, u: float, v: float) -> float:
        ty, tx = self._sigma_tiles.shape
        iy = min(ty - 1, max(0, int(v * ty / self.height)))
        ix = min(tx - 1, max(0, int(u * tx / self.width)))
        return float(self._sigma_tiles[iy, ix])

    def _expand(self, bbox: Tuple[int, int, int, int], margin: int) -> Tuple[int, int, int, int]:
        x, y, w, h = bbox
        x0, y0 = max(0, x - margin), max(0, y - margin)
        x1, y1 = min(self.width, x + w + margin), min(self.height, y + h + margin)
        return y0, y1, x0, x1

    def _measure(self, y: np.ndarray, diff: Any, low: np.ndarray, seeds: np.ndarray) -> List[Blob]:
        n, labels, stats, _ = cv2.connectedComponentsWithStats(low, connectivity=8)
        if n <= 1 or not seeds.any():
            return []
        seed_labels = labels[seeds]
        seed_labels = seed_labels[seed_labels > 0]
        if seed_labels.size == 0:
            return []
        selected = np.unique(seed_labels)
        if selected.size > self.cfg.max_blobs:
            areas = stats[selected, cv2.CC_STAT_AREA]
            selected = selected[np.argsort(-areas)[: self.cfg.max_blobs]]
        d = self.d
        blobs: List[Blob] = []
        for lab in selected:
            x, yy, w, h = (int(stats[lab, i]) for i in (cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP,
                                                         cv2.CC_STAT_WIDTH, cv2.CC_STAT_HEIGHT))
            y0, y1, x0, x1 = self._expand((x * d, yy * d, w * d, h * d), 1)
            blob = self._measure_roi(y, diff, y0, y1, x0, x1)
            if blob is not None:
                blobs.append(blob)
        return blobs

    def _measure_roi(self, y: np.ndarray, diff: Any, y0: int, y1: int, x0: int, x1: int) -> Optional[Blob]:
        roi = self._to_host(diff[y0:y1, x0:x1]).astype(np.float32)
        sigma = self._local_sigma((x0 + x1) / 2.0, (y0 + y1) / 2.0)
        thr_low = max(self.cfg.low_threshold_sigma * sigma, self.cfg.min_threshold_dn)
        inside = roi > thr_low
        area = int(np.count_nonzero(inside))
        if area == 0:
            return None
        floor = _WEIGHT_FLOOR_SIGMA * sigma
        wts = np.where(inside, roi - floor, 0.0).clip(min=0.0)
        flux = float(wts.sum())
        if flux <= 0.0:
            return None
        u = x0 + float(wts.sum(axis=0) @ np.arange(x1 - x0, dtype=np.float64)) / flux
        v = y0 + float(wts.sum(axis=1) @ np.arange(y1 - y0, dtype=np.float64)) / flux
        peak = float(roi.max())
        signed = y[y0:y1, x0:x1].astype(np.float32) - self._bg_roi(y0, y1, x0, x1)
        polarity = 1 if float(signed[inside].sum()) >= 0.0 else -1
        return Blob(
            u=u, v=v, area=area, bbox=(x0, y0, x1 - x0, y1 - y0), peak=peak,
            mean=float(roi[inside].mean()), flux=flux, eff_area=flux / max(peak - floor, 1e-3),
            snr=peak / sigma, sigma=sigma, polarity=polarity,
            relative_intensity=min(1.0, peak / self._dyn_range),
        )


class CpuDetector(Detector):
    """OpenCV implementation (SIMD and multi-threaded inside OpenCV)."""

    name = "cpu"

    def __init__(self, cfg: DetectionConfig, width: int, height: int, fps: float):
        super().__init__(cfg, width, height, fps)
        self._bg: Optional[np.ndarray] = None
        self._bg_u8: Optional[np.ndarray] = None
        self._pool_kernel = np.ones((self.d, self.d), np.uint8)
        self._low_map: Optional[np.ndarray] = None
        self._high_map: Optional[np.ndarray] = None

    def _reset_background(self, y: np.ndarray) -> None:
        self._bg = y.astype(np.float32)
        self._bg_u8 = y.copy()
        if self._low_map is None:
            self._set_threshold_maps(self._threshold_map(self.cfg.low_threshold_sigma),
                                     self._threshold_map(self.cfg.threshold_sigma))

    def _absdiff(self, y: np.ndarray) -> np.ndarray:
        return cv2.absdiff(y, self._bg_u8)

    def _maxpool(self, diff: np.ndarray) -> np.ndarray:
        if self.d == 1:
            return diff
        pooled = cv2.dilate(diff[: self.hc, : self.wc], self._pool_kernel, anchor=(0, 0))
        return np.ascontiguousarray(pooled[:: self.d, :: self.d])

    def _to_host(self, arr: np.ndarray) -> np.ndarray:
        return arr

    def _set_threshold_maps(self, low: np.ndarray, high: np.ndarray) -> None:
        self._low_map, self._high_map = low, high

    def _threshold(self, dec: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        low = cv2.compare(dec, self._low_map, cv2.CMP_GT)
        seeds = cv2.compare(dec, self._high_map, cv2.CMP_GT) > 0
        return low, seeds

    def _bg_roi(self, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        return self._bg[y0:y1, x0:x1]

    def _update_background(self, y: np.ndarray, rois: Sequence[Tuple[int, int, int, int]]) -> None:
        saved = [(r, self._bg[r[0]:r[1], r[2]:r[3]].copy()) for r in rois]
        cv2.accumulateWeighted(y, self._bg, self.alpha)
        slow = self.alpha * self.cfg.foreground_alpha_scale
        for (y0, y1, x0, x1), old in saved:
            self._bg[y0:y1, x0:x1] = old + slow * (y[y0:y1, x0:x1].astype(np.float32) - old)
        cv2.convertScaleAbs(self._bg, self._bg_u8)


class ArrayDetector(Detector):
    """Per-pixel stages on any NumPy-compatible array module (CuPy on the GPU).

    With ``xp=cupy`` the background, difference, pooling and thresholding
    run as CUDA kernels; only the decimated masks (~0.5 MB) and blob ROIs
    come back to the host. On Jetson, CPU and GPU share DRAM, but CuPy still
    copies between host and device allocations, which is why transfers are
    kept to these small arrays. NOT hardware-tested.
    """

    name = "cuda"

    def __init__(self, cfg: DetectionConfig, width: int, height: int, fps: float, xp: Any):
        super().__init__(cfg, width, height, fps)
        self.xp = xp
        self._bg = None
        self._bg_u8 = None
        self._low_map = None
        self._high_map = None
        self._y_dev = None

    def _host(self, arr: Any) -> np.ndarray:
        return arr.get() if hasattr(arr, "get") else np.asarray(arr)

    def _upload(self, y: np.ndarray) -> Any:
        self._y_dev = self.xp.asarray(y)
        return self._y_dev

    def _reset_background(self, y: np.ndarray) -> None:
        y_dev = self._upload(y)
        self._bg = y_dev.astype(self.xp.float32)
        self._bg_u8 = y_dev.copy()
        if self._low_map is None:
            self._set_threshold_maps(self._threshold_map(self.cfg.low_threshold_sigma),
                                     self._threshold_map(self.cfg.threshold_sigma))

    def _absdiff(self, y: np.ndarray) -> Any:
        xp = self.xp
        y_dev = self._upload(y)
        return xp.abs(y_dev.astype(xp.int16) - self._bg_u8.astype(xp.int16)).astype(xp.uint8)

    def _maxpool(self, diff: Any) -> Any:
        if self.d == 1:
            return diff
        hd, wd = self.dec_shape
        return diff[: self.hc, : self.wc].reshape(hd, self.d, wd, self.d).max(axis=(1, 3))

    def _to_host(self, arr: Any) -> np.ndarray:
        return self._host(arr)

    def _set_threshold_maps(self, low: np.ndarray, high: np.ndarray) -> None:
        self._low_map, self._high_map = self.xp.asarray(low), self.xp.asarray(high)

    def _threshold(self, dec: Any) -> Tuple[np.ndarray, np.ndarray]:
        xp = self.xp
        low = self._host(xp.where(dec > self._low_map, 255, 0).astype(xp.uint8))
        seeds = self._host(dec > self._high_map)
        return low, seeds

    def _bg_roi(self, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        return self._host(self._bg[y0:y1, x0:x1])

    def _update_background(self, y: np.ndarray, rois: Sequence[Tuple[int, int, int, int]]) -> None:
        xp = self.xp
        y_dev = self._y_dev.astype(xp.float32)
        saved = [(r, self._bg[r[0]:r[1], r[2]:r[3]].copy()) for r in rois]
        self._bg += self.alpha * (y_dev - self._bg)
        slow = self.alpha * self.cfg.foreground_alpha_scale
        for (y0, y1, x0, x1), old in saved:
            self._bg[y0:y1, x0:x1] = old + slow * (y_dev[y0:y1, x0:x1] - old)
        self._bg_u8 = xp.clip(xp.rint(self._bg), 0, 255).astype(xp.uint8)


def cuda_array_module() -> Optional[Any]:
    """CuPy if it is installed and can run a kernel on a CUDA device, else None."""
    try:
        import cupy  # type: ignore[import-not-found]

        if cupy.cuda.runtime.getDeviceCount() < 1:
            return None
        if int(cupy.arange(4, dtype=cupy.int32).sum()) != 6:  # compiles and runs a real kernel
            return None
        return cupy
    except Exception as exc:  # ImportError, CUDA driver/runtime errors
        log.debug("CuPy unavailable: %s", exc)
        return None


def make_detector(cfg: DetectionConfig, width: int, height: int, fps: float) -> Detector:
    """Build the configured backend; ``auto`` prefers CUDA and falls back to CPU."""
    if cfg.backend in ("auto", "cuda"):
        xp = cuda_array_module()
        if xp is not None:
            log.info("detection backend: cuda (CuPy)")
            return ArrayDetector(cfg, width, height, fps, xp)
        if cfg.backend == "cuda":
            log.warning("detection backend 'cuda' requested but CuPy/CUDA is not usable; using cpu")
    log.info("detection backend: cpu (OpenCV %s, %d threads)", cv2.__version__, cv2.getNumThreads())
    return CpuDetector(cfg, width, height, fps)
