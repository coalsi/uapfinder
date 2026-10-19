// TypeScript port of the parts of common/skynet_common/geodesy.py the
// dashboard needs. Same conventions: WGS84, heights above the ellipsoid,
// azimuth clockwise from true north, elevation above the geodetic horizon.

export type Vec3 = readonly [number, number, number];

export const WGS84_A = 6378137.0;
export const WGS84_F = 1.0 / 298.257223563;
export const WGS84_E2 = WGS84_F * (2.0 - WGS84_F);
export const EARTH_MEAN_RADIUS_M = 6371008.8;
export const METERS_PER_MILE = 1609.344;

const DEG = Math.PI / 180;

export const toRad = (deg: number): number => deg * DEG;
export const toDeg = (rad: number): number => rad / DEG;

export function dot(a: Vec3, b: Vec3): number {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

export function cross(a: Vec3, b: Vec3): Vec3 {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}

export function sub(a: Vec3, b: Vec3): Vec3 {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}

export function norm(a: Vec3): number {
  return Math.hypot(a[0], a[1], a[2]);
}

export function normalize(a: Vec3): Vec3 {
  const n = norm(a);
  if (n === 0) throw new RangeError("cannot normalise a zero vector");
  return [a[0] / n, a[1] / n, a[2] / n];
}

/** Angle in degrees between two vectors, accurate near 0 and 180 degrees. */
export function angleBetweenDeg(a: Vec3, b: Vec3): number {
  return toDeg(Math.atan2(norm(cross(a, b)), dot(a, b)));
}

export function geodeticToEcef(latDeg: number, lonDeg: number, hM: number): Vec3 {
  const lat = toRad(latDeg);
  const lon = toRad(lonDeg);
  const sinLat = Math.sin(lat);
  const cosLat = Math.cos(lat);
  const n = WGS84_A / Math.sqrt(1.0 - WGS84_E2 * sinLat * sinLat);
  return [
    (n + hM) * cosLat * Math.cos(lon),
    (n + hM) * cosLat * Math.sin(lon),
    (n * (1.0 - WGS84_E2) + hM) * sinLat,
  ];
}

/** ECEF unit vectors (east, north, up) at a geodetic location. */
export function enuBasis(latDeg: number, lonDeg: number): [Vec3, Vec3, Vec3] {
  const lat = toRad(latDeg);
  const lon = toRad(lonDeg);
  const sl = Math.sin(lat);
  const cl = Math.cos(lat);
  const so = Math.sin(lon);
  const co = Math.cos(lon);
  return [
    [-so, co, 0.0],
    [-sl * co, -sl * so, cl],
    [cl * co, cl * so, sl],
  ];
}

export function azelToEnu(azDeg: number, elDeg: number): Vec3 {
  const az = toRad(azDeg);
  const el = toRad(elDeg);
  const ce = Math.cos(el);
  return [ce * Math.sin(az), ce * Math.cos(az), Math.sin(el)];
}

/** Azimuth in [0, 360) and elevation of an ENU vector (need not be unit). */
export function enuToAzel(e: number, n: number, u: number): [number, number] {
  const az = ((toDeg(Math.atan2(e, n)) % 360) + 360) % 360;
  const el = toDeg(Math.atan2(u, Math.hypot(e, n)));
  return [az, el];
}

/** Azimuth (deg), elevation (deg) and slant range (m) from observer to target. */
export function lookAngles(
  obsLat: number,
  obsLon: number,
  obsH: number,
  tgtLat: number,
  tgtLon: number,
  tgtH: number,
): [number, number, number] {
  const d = sub(geodeticToEcef(tgtLat, tgtLon, tgtH), geodeticToEcef(obsLat, obsLon, obsH));
  const [east, north, up] = enuBasis(obsLat, obsLon);
  const [az, el] = enuToAzel(dot(d, east), dot(d, north), dot(d, up));
  return [az, el, norm(d)];
}

/** Great-circle surface distance in metres on a mean-radius sphere. */
export function haversineM(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const p1 = toRad(lat1);
  const p2 = toRad(lat2);
  const dp = p2 - p1;
  const dl = toRad(lon2 - lon1);
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2.0 * EARTH_MEAN_RADIUS_M * Math.asin(Math.min(1.0, Math.sqrt(a)));
}
