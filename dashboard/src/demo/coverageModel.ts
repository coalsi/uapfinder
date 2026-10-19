// Client-side stand-in for GET /api/v1/coverage, following the semantics in
// docs/API.md, so demo mode exercises the same rendering path.

import type { Coverage, CoverageCell, CoverageQuery, CoverageStatus, CoverageSuggestion, Node } from "../api/types";
import { type Attitude, PinholeCamera, azelToPixel } from "../geo/camera";
import { type Vec3, METERS_PER_MILE, angleBetweenDeg, geodeticToEcef, haversineM, lookAngles, sub } from "../geo/geodesy";

const MAX_RANGE_M = 80 * METERS_PER_MILE;
const KM_PER_DEG_LAT = 111.32;

export interface Viewer {
  lat: number;
  lon: number;
  alt: number;
  ecef: Vec3;
  cam: PinholeCamera;
  attitude: Attitude;
}

export function viewerFromNode(node: Node): Viewer | null {
  if (
    !node.online ||
    node.lat == null ||
    node.lon == null ||
    node.alt_m == null ||
    node.yaw_deg == null ||
    node.pitch_deg == null ||
    node.fov_h_deg == null
  ) {
    return null;
  }
  return {
    lat: node.lat,
    lon: node.lon,
    alt: node.alt_m,
    ecef: geodeticToEcef(node.lat, node.lon, node.alt_m),
    cam: PinholeCamera.fromFov(1920, 1080, node.fov_h_deg, node.fov_v_deg),
    attitude: { yawDeg: node.yaw_deg, pitchDeg: node.pitch_deg, rollDeg: node.roll_deg ?? 0 },
  };
}

/** Does `v` see the point (within 80 miles horizontally, above the elevation mask, in the field of view)? */
export function sees(v: Viewer, lat: number, lon: number, alt: number, minElevationDeg: number): boolean {
  if (haversineM(v.lat, v.lon, lat, lon) > MAX_RANGE_M) return false;
  const [az, el] = lookAngles(v.lat, v.lon, v.alt, lat, lon, alt);
  if (el < minElevationDeg) return false;
  const uv = azelToPixel(v.cam, az, el, v.attitude);
  return uv !== null && v.cam.inImage(uv[0], uv[1]);
}

export function parallaxDeg(a: Vec3, b: Vec3, p: Vec3): number {
  return angleBetweenDeg(sub(a, p), sub(b, p));
}

interface Cell {
  lat: number;
  lon: number;
  ecef: Vec3;
  ring: [number, number][];
  seers: Viewer[];
  stereoPairs: number;
  status: CoverageStatus;
}

function classify(seers: Viewer[], p: Vec3, minParallaxDeg: number): { status: CoverageStatus; pairs: number } {
  let pairs = 0;
  for (let i = 0; i < seers.length; i++) {
    for (let j = i + 1; j < seers.length; j++) {
      if (parallaxDeg((seers[i] as Viewer).ecef, (seers[j] as Viewer).ecef, p) >= minParallaxDeg) pairs++;
    }
  }
  if (pairs > 0) return { status: "stereo", pairs };
  return { status: seers.length > 0 ? "mono" : "blind", pairs };
}

const SUGGESTION_CAMERA = PinholeCamera.fromFov(1000, 1000, 90, 90);
const ZENITH: Attitude = { yawDeg: 0, pitchDeg: 90, rollDeg: 0 };

export function computeCoverage(nodes: Node[], q: CoverageQuery): Coverage {
  const minEl = q.min_elevation_deg ?? 10;
  const minParallax = q.min_parallax_deg ?? 5;
  const viewers = nodes.map(viewerFromNode).filter((v): v is Viewer => v !== null);
  const stepLat = q.cell_km / KM_PER_DEG_LAT;
  const midLat = (q.min_lat + q.max_lat) / 2;
  const stepLon = q.cell_km / (KM_PER_DEG_LAT * Math.cos((midLat * Math.PI) / 180));
  const rows = Math.max(1, Math.round((q.max_lat - q.min_lat) / stepLat));
  const cols = Math.max(1, Math.round((q.max_lon - q.min_lon) / stepLon));
  if (rows * cols > 10_000) throw new Error("grid exceeds 10000 cells");

  const cells: Cell[] = [];
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const s = q.min_lat + r * stepLat;
      const w = q.min_lon + c * stepLon;
      const lat = s + stepLat / 2;
      const lon = w + stepLon / 2;
      const ecef = geodeticToEcef(lat, lon, q.ref_alt_m);
      const seers = viewers.filter((v) => sees(v, lat, lon, q.ref_alt_m, minEl));
      const { status, pairs } = classify(seers, ecef, minParallax);
      cells.push({
        lat,
        lon,
        ecef,
        seers,
        stereoPairs: pairs,
        status,
        ring: [
          [w, s],
          [w + stepLon, s],
          [w + stepLon, s + stepLat],
          [w, s + stepLat],
          [w, s],
        ],
      });
    }
  }

  const summary: Record<CoverageStatus, number> = { blind: 0, mono: 0, stereo: 0 };
  for (const cell of cells) summary[cell.status]++;

  return {
    type: "FeatureCollection",
    params: { ...q, min_elevation_deg: minEl, min_parallax_deg: minParallax, source: "demo" },
    summary,
    suggestions: suggest(cells, q, minEl, minParallax),
    features: cells.map(
      (cell): CoverageCell => ({
        type: "Feature",
        geometry: { type: "Polygon", coordinates: [cell.ring] },
        properties: { seeing: cell.seers.length, stereo_pairs: cell.stereoPairs, status: cell.status },
      }),
    ),
  };
}

/** Up to 10 cell centres where a zenith-pointing 90-degree node would turn the most cells stereo. */
function suggest(cells: Cell[], q: CoverageQuery, minEl: number, minParallax: number): CoverageSuggestion[] {
  const upgradable = cells.filter((c) => c.status === "mono");
  if (upgradable.length === 0) return [];
  const stride = Math.max(1, Math.ceil(Math.sqrt(cells.length / 400)));
  const scored: CoverageSuggestion[] = [];
  cells.forEach((site, i) => {
    if (i % stride !== 0) return;
    const viewer: Viewer = {
      lat: site.lat,
      lon: site.lon,
      alt: 0,
      ecef: geodeticToEcef(site.lat, site.lon, 0),
      cam: SUGGESTION_CAMERA,
      attitude: ZENITH,
    };
    let gain = 0;
    for (const cell of upgradable) {
      if (!sees(viewer, cell.lat, cell.lon, q.ref_alt_m, minEl)) continue;
      if (cell.seers.some((s) => parallaxDeg(s.ecef, viewer.ecef, cell.ecef) >= minParallax)) gain++;
    }
    if (gain > 0) scored.push({ lat: site.lat, lon: site.lon, gain_cells: gain });
  });
  scored.sort((a, b) => b.gain_cells - a.gain_cells);
  const minSeparationM = 2.5 * q.cell_km * 1000 * stride;
  const picked: CoverageSuggestion[] = [];
  for (const s of scored) {
    if (picked.some((p) => haversineM(p.lat, p.lon, s.lat, s.lon) < minSeparationM)) continue;
    picked.push({ lat: Math.round(s.lat * 1e4) / 1e4, lon: Math.round(s.lon * 1e4) / 1e4, gain_cells: s.gain_cells });
    if (picked.length === 10) break;
  }
  return picked;
}
