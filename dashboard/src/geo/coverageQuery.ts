import type { CoverageQuery } from "../api/types";

export interface GeoRect {
  west: number;
  south: number;
  east: number;
  north: number;
}

const KM_PER_DEG_LAT = 111.32;
/** Cells per request we aim for; the server rejects more than 10 000. */
export const TARGET_CELLS = 2500;
export const SERVER_MAX_CELLS = 10_000;
/** Largest box requested; beyond ~1300 km nothing is within 80 miles of a typical cluster anyway. */
const MAX_SPAN_LAT_DEG = 12;
const MAX_SPAN_LON_DEG = 16;
const NICE_CELL_KM = [2, 5, 10, 20, 25, 50] as const;
const MAX_CELL_KM = 100;

export function cellCount(rect: GeoRect, cellKm: number): number {
  const midLat = (rect.south + rect.north) / 2;
  const kmPerDegLon = KM_PER_DEG_LAT * Math.max(0.01, Math.cos((midLat * Math.PI) / 180));
  const rows = Math.ceil(((rect.north - rect.south) * KM_PER_DEG_LAT) / cellKm);
  const cols = Math.ceil(((rect.east - rect.west) * kmPerDegLon) / cellKm);
  return rows * cols;
}

/** Smallest "nice" cell size that keeps the grid near {@link TARGET_CELLS}. */
export function cellSizeFor(rect: GeoRect): number {
  for (const km of NICE_CELL_KM) {
    if (cellCount(rect, km) <= TARGET_CELLS) return km;
  }
  return MAX_CELL_KM;
}

function clampSpan(lo: number, hi: number, maxSpan: number, min: number, max: number): [number, number] {
  if (hi - lo <= maxSpan) return [Math.max(min, lo), Math.min(max, hi)];
  const mid = (lo + hi) / 2;
  return [Math.max(min, mid - maxSpan / 2), Math.min(max, mid + maxSpan / 2)];
}

/**
 * Coverage request for the visible area: the view rectangle is clamped to a
 * sane maximum, the cell size grows with the area, and the box is snapped
 * outwards to the cell grid so panning does not shift the cells.
 */
export function coverageQueryForView(view: GeoRect, refAltM: number): CoverageQuery {
  // A view crossing the antimeridian (west > east) is cut at 180 degrees.
  const east = view.east < view.west ? 180 : view.east;
  const [south, north] = clampSpan(view.south, view.north, MAX_SPAN_LAT_DEG, -85, 85);
  const [west, east2] = clampSpan(view.west, east, MAX_SPAN_LON_DEG, -180, 180);
  const rect = { west, south, east: east2, north };
  const cellKm = cellSizeFor(rect);
  const stepLat = cellKm / KM_PER_DEG_LAT;
  const stepLon = cellKm / (KM_PER_DEG_LAT * Math.max(0.01, Math.cos((((south + north) / 2) * Math.PI) / 180)));
  const snapped = {
    min_lat: Math.max(-85, Math.floor(south / stepLat) * stepLat),
    max_lat: Math.min(85, Math.ceil(north / stepLat) * stepLat),
    min_lon: Math.max(-180, Math.floor(west / stepLon) * stepLon),
    max_lon: Math.min(180, Math.ceil(east2 / stepLon) * stepLon),
  };
  const round = (v: number) => Math.round(v * 1e5) / 1e5;
  return {
    min_lat: round(snapped.min_lat),
    min_lon: round(snapped.min_lon),
    max_lat: round(snapped.max_lat),
    max_lon: round(snapped.max_lon),
    cell_km: cellKm,
    ref_alt_m: refAltM,
  };
}

/** Stable key for de-duplicating identical requests. */
export function coverageQueryKey(q: CoverageQuery): string {
  return [q.min_lat, q.min_lon, q.max_lat, q.max_lon, q.cell_km, q.ref_alt_m].join(",");
}
