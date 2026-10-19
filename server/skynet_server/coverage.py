"""Stereo coverage maps (``GET /api/v1/coverage``) and siting suggestions.

The bounding box is cut into cells of about ``cell_km``; for the point
``ref_alt_m`` above each cell centre and every active node with known camera
geometry we evaluate, vectorised over all cells:

* range: great-circle distance from the node at most ``radius_m`` (80 mi);
* elevation of the point above the node's horizon >= ``min_elevation_deg``;
* field of view: the direction projects inside the pinhole image of the
  node's camera (yaw/pitch/roll and horizontal/vertical FOV, the same model
  as ``skynet_common.camera``).

A cell is ``stereo`` when some pair of nodes that see it views the point
with a parallax angle >= ``min_parallax_deg``, ``mono`` when it is seen at all,
``blind`` otherwise. Pairs are only evaluated on the cells both nodes see.

``suggestions`` are a greedy placement of new zenith-pointing nodes (90 deg
square FOV) at cell centres: each pick maximises the number of currently
non-stereo cells it would make stereo (paired with an existing node or an
earlier pick), so successive suggestions spread out instead of piling onto
the same gap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from skynet_common.camera import camera_basis_enu
from skynet_common.geodesy import EARTH_MEAN_RADIUS_M, WGS84_A, WGS84_E2

from .models import public_position

MAX_CELLS = 10_000
MAX_SUGGESTIONS = 10
_MAX_CANDIDATE_SITES = 150
_SUGGESTION_FOV_DEG = 90.0


class CoverageError(ValueError):
    """Bad coverage request parameters (HTTP 400)."""


@dataclass(frozen=True)
class CameraNode:
    lat: float
    lon: float
    alt_m: float
    yaw_deg: float
    pitch_deg: float
    roll_deg: float
    fov_h_deg: float
    fov_v_deg: float


def _ecef(lat_deg: np.ndarray, lon_deg: np.ndarray, h_m: np.ndarray) -> np.ndarray:
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    sl, cl = np.sin(lat), np.cos(lat)
    n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sl * sl)
    return np.stack([(n + h_m) * cl * np.cos(lon), (n + h_m) * cl * np.sin(lon), (n * (1.0 - WGS84_E2) + h_m) * sl],
                    axis=-1)


def _enu_basis(lat_deg: float, lon_deg: float) -> np.ndarray:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    sl, cl, so, co = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    return np.array([[-so, co, 0.0], [-sl * co, -sl * so, cl], [cl * co, cl * so, sl]])  # rows: east, north, up


def _haversine_m(lat1: float, lon1: float, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    p1, p2 = math.radians(lat1), np.radians(lat2)
    dp, dl = p2 - p1, np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + math.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2.0 * EARTH_MEAN_RADIUS_M * np.arcsin(np.minimum(1.0, np.sqrt(a)))


class _Grid:
    def __init__(self, min_lat: float, min_lon: float, max_lat: float, max_lon: float, cell_km: float):
        if not (-90 <= min_lat < max_lat <= 90) or not (-180 <= min_lon < max_lon <= 180):
            raise CoverageError("need -90 <= min_lat < max_lat <= 90 and -180 <= min_lon < max_lon <= 180")
        if not (0.1 <= cell_km <= 500):
            raise CoverageError("cell_km must be between 0.1 and 500")
        mid = math.radians((min_lat + max_lat) / 2.0)
        self.dlat = cell_km / 111.32
        self.dlon = cell_km / (111.32 * max(math.cos(mid), 1e-3))
        self.rows = max(1, math.ceil((max_lat - min_lat) / self.dlat - 1e-9))
        self.cols = max(1, math.ceil((max_lon - min_lon) / self.dlon - 1e-9))
        if self.rows * self.cols > MAX_CELLS:
            raise CoverageError(f"grid of {self.rows}x{self.cols} cells exceeds {MAX_CELLS}; increase cell_km")
        self.min_lat, self.min_lon, self.max_lat, self.max_lon = min_lat, min_lon, max_lat, max_lon
        i, j = np.meshgrid(np.arange(self.rows), np.arange(self.cols), indexing="ij")
        self.lat0 = (min_lat + i * self.dlat).ravel()
        self.lon0 = (min_lon + j * self.dlon).ravel()
        self.lat1 = np.minimum(self.lat0 + self.dlat, max_lat)
        self.lon1 = np.minimum(self.lon0 + self.dlon, max_lon)
        self.lat = (self.lat0 + self.lat1) / 2.0
        self.lon = (self.lon0 + self.lon1) / 2.0

    @property
    def size(self) -> int:
        return self.lat.size


def _sees(node: CameraNode, cells_ecef: np.ndarray, cell_lat: np.ndarray, cell_lon: np.ndarray,
          radius_m: float, min_el_deg: float) -> Tuple[np.ndarray, np.ndarray]:
    """(visible mask, unit vectors node -> point) for one node over all cells."""
    origin = _ecef(np.array(node.lat), np.array(node.lon), np.array(node.alt_m))
    d = cells_ecef - origin
    dist = np.linalg.norm(d, axis=1)
    u = d / np.maximum(dist, 1e-9)[:, None]
    enu = u @ _enu_basis(node.lat, node.lon).T  # columns: e, n, up
    el = np.degrees(np.arctan2(enu[:, 2], np.hypot(enu[:, 0], enu[:, 1])))
    right, down, forward = (np.array(v) for v in camera_basis_enu(node.yaw_deg, node.pitch_deg, node.roll_deg))
    z = enu @ forward
    with np.errstate(divide="ignore", invalid="ignore"):
        in_fov = ((z > 1e-9)
                  & (np.abs((enu @ right) / z) <= math.tan(math.radians(node.fov_h_deg) / 2.0))
                  & (np.abs((enu @ down) / z) <= math.tan(math.radians(node.fov_v_deg) / 2.0)))
    near = _haversine_m(node.lat, node.lon, cell_lat, cell_lon) <= radius_m
    return near & (el >= min_el_deg) & in_fov, u


def compute_coverage(nodes: Sequence[CameraNode], min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                     cell_km: float = 10.0, ref_alt_m: float = 3000.0, min_elevation_deg: float = 10.0,
                     min_parallax_deg: float = 5.0, radius_m: float = 128_747.52) -> Dict[str, Any]:
    if not (-500.0 <= ref_alt_m <= 100_000.0):
        raise CoverageError("ref_alt_m must be between -500 and 100000")
    if not (0.0 <= min_parallax_deg < 180.0) or not (-90.0 <= min_elevation_deg <= 90.0):
        raise CoverageError("min_parallax_deg must be in [0, 180) and min_elevation_deg in [-90, 90]")
    grid = _Grid(min_lat, min_lon, max_lat, max_lon, cell_km)
    cells = _ecef(grid.lat, grid.lon, np.full(grid.size, ref_alt_m))
    cos_min = math.cos(math.radians(min_parallax_deg))

    masks: List[np.ndarray] = []
    units: List[np.ndarray] = []
    for node in nodes:
        mask, u = _sees(node, cells, grid.lat, grid.lon, radius_m, min_elevation_deg)
        if mask.any():
            masks.append(mask)
            units.append(u)
    seeing = np.sum(masks, axis=0) if masks else np.zeros(grid.size, dtype=int)
    pairs = np.zeros(grid.size, dtype=int)
    for a in range(len(masks)):
        for b in range(a + 1, len(masks)):
            both = masks[a] & masks[b]
            if both.any():
                dots = np.einsum("ij,ij->i", units[a][both], units[b][both])
                pairs[both] += dots <= cos_min
    stereo = pairs > 0
    status = np.where(stereo, "stereo", np.where(seeing > 0, "mono", "blind"))

    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[
                [round(float(lo0), 6), round(float(la0), 6)], [round(float(lo1), 6), round(float(la0), 6)],
                [round(float(lo1), 6), round(float(la1), 6)], [round(float(lo0), 6), round(float(la1), 6)],
                [round(float(lo0), 6), round(float(la0), 6)],
            ]]},
            "properties": {"seeing": int(n), "stereo_pairs": int(p), "status": str(s)},
        }
        for la0, lo0, la1, lo1, n, p, s in zip(grid.lat0, grid.lon0, grid.lat1, grid.lon1, seeing, pairs, status)
    ]
    alt = float(np.median([n.alt_m for n in nodes])) if nodes else 0.0
    return {
        "type": "FeatureCollection",
        "params": {
            "min_lat": min_lat, "min_lon": min_lon, "max_lat": max_lat, "max_lon": max_lon, "cell_km": cell_km,
            "ref_alt_m": ref_alt_m, "min_elevation_deg": min_elevation_deg, "min_parallax_deg": min_parallax_deg,
            "radius_m": radius_m, "nodes": len(nodes), "rows": grid.rows, "cols": grid.cols,
        },
        "summary": {"blind": int(np.sum(status == "blind")), "mono": int(np.sum(status == "mono")),
                    "stereo": int(np.sum(stereo))},
        "suggestions": _suggest(grid, cells, masks, units, stereo, alt, radius_m, min_elevation_deg, cos_min),
        "features": features,
    }


def _suggest(grid: _Grid, cells: np.ndarray, masks: List[np.ndarray], units: List[np.ndarray], stereo: np.ndarray,
             site_alt_m: float, radius_m: float, min_el_deg: float, cos_min: float) -> List[Dict[str, Any]]:
    """Greedy placement of up to ten zenith-pointing nodes at cell centres.

    Candidate sites are a sub-lattice of the cell centres (at most
    ``_MAX_CANDIDATE_SITES``), evaluated together: ``gain[k, c]`` is True when
    a new node at site k would make cell c stereo with some node already
    placed (existing or picked).
    """
    if stereo.all():
        return []
    step = max(1, math.ceil(math.sqrt(grid.size / _MAX_CANDIDATE_SITES)))
    sites = (np.arange(0, grid.rows, step)[:, None] * grid.cols + np.arange(0, grid.cols, step)[None, :]).ravel()
    cand_masks = np.zeros((sites.size, grid.size), dtype=bool)
    cand_units = np.zeros((sites.size, grid.size, 3), dtype=np.float32)
    for k, idx in enumerate(sites):
        node = CameraNode(float(grid.lat[idx]), float(grid.lon[idx]), site_alt_m, 0.0, 90.0, 0.0,
                          _SUGGESTION_FOV_DEG, _SUGGESTION_FOV_DEG)
        cand_masks[k], cand_units[k] = _sees(node, cells, grid.lat, grid.lon, radius_m, min_el_deg)

    def gains_with(mask: np.ndarray, unit: np.ndarray) -> np.ndarray:
        both = cand_masks & mask[None, :]
        return both & (np.einsum("kcj,cj->kc", cand_units, unit.astype(np.float32)) <= cos_min)

    gain = np.zeros((sites.size, grid.size), dtype=bool)
    for mask, unit in zip(masks, units):
        gain |= gains_with(mask, unit)
    covered = stereo.copy()
    picks: List[Dict[str, Any]] = []
    for _ in range(MAX_SUGGESTIONS):
        score = (gain & ~covered).sum(axis=1)
        best = int(np.argmax(score))
        if score[best] <= 0:
            break
        idx = int(sites[best])
        picks.append({"lat": round(float(grid.lat[idx]), 4), "lon": round(float(grid.lon[idx]), 4),
                      "gain_cells": int(score[best])})
        covered |= gain[best]
        gain |= gains_with(cand_masks[best], cand_units[best])
    return picks


def camera_nodes(records: Sequence[Any], now_ns: int, active_ns: int, decimals: int) -> List[CameraNode]:
    """Active nodes with complete camera geometry, from NodeRecords.

    Positions are the rounded public ones: the edges of a node's coverage
    (its range and minimum-elevation circles, its field of view) on a fine
    grid would otherwise reveal where the camera stands.
    """
    out = []
    for n in records:
        values: List[Optional[float]] = [n.lat, n.lon, n.alt_m, n.yaw_deg, n.pitch_deg, n.roll_deg, n.fov_h_deg,
                                         n.fov_v_deg]
        if n.last_seen_ns and now_ns - n.last_seen_ns <= active_ns and all(v is not None for v in values):
            lat, lon, alt = public_position(n.lat, n.lon, n.alt_m, decimals)
            out.append(CameraNode(lat, lon, alt, *values[3:]))  # type: ignore[arg-type]
    return out
