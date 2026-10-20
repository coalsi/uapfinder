/**
 * Geometry for the "two nodes as a pair" diagram on /device/, shared by the
 * build-time render and the optional animation, so both draw exactly the same
 * thing. Everything is a vertical slice through both nodes (a side view), in
 * kilometres: x along the baseline from node A, z up. Both cameras point
 * straight up with a 90° field of view, so their image x axes are parallel to
 * the baseline and a star (at infinity) lands on the same pixel in both
 * frames, while a nearby object does not: that offset is the parallax.
 */

export type P = [number, number];

export const BASELINE_KM = 12; // ≈ 7.5 miles, inside the 5–40 mile range
export const FOV_DEG = 90;
/** Angular error drawn around each sight line. Real nodes: 0.01–0.05°; exaggerated so it is visible. */
export const DRAWN_ERROR_DEG = 1.4;

export const A: P = [0, 0];
export const B: P = [BASELINE_KM, 0];

/** Object position at phase s ∈ [0, 1): a gentle loop inside the overlap of both fields of view. */
export function objectAt(s: number): P {
  const a = 2 * Math.PI * s;
  return [6 + 2.2 * Math.sin(a), 9.6 + 0.9 * Math.sin(2 * a)];
}

const sub = (p: P, q: P): P => [p[0] - q[0], p[1] - q[1]];
const add = (p: P, q: P): P => [p[0] + q[0], p[1] + q[1]];
const mul = (p: P, k: number): P => [p[0] * k, p[1] * k];
const len = (p: P) => Math.hypot(p[0], p[1]);
const unit = (p: P): P => mul(p, 1 / len(p));
const rot = (p: P, a: number): P => [p[0] * Math.cos(a) - p[1] * Math.sin(a), p[0] * Math.sin(a) + p[1] * Math.cos(a)];

/** Intersection of the lines p + t·u and q + s·v. */
export function meet(p: P, u: P, q: P, v: P): P {
  const det = u[0] * -v[1] - u[1] * -v[0];
  const d = sub(q, p);
  const t = (d[0] * -v[1] - d[1] * -v[0]) / det;
  return add(p, mul(u, t));
}

export interface PairGeometry {
  object: P;
  /** Corners of the region where both (exaggerated) error wedges overlap. */
  region: P[];
  /** Ends of the wedges drawn from each node, as [left edge end, right edge end]. */
  wedgeA: [P, P];
  wedgeB: [P, P];
  /** Parallax angle at the object, degrees. */
  parallaxDeg: number;
  /** Image coordinates in [-1, 1] across the frame width (0 = centre) for each camera. */
  imageA: number;
  imageB: number;
  rangeA: number;
  rangeB: number;
}

export function pairGeometry(s: number): PairGeometry {
  const O = objectAt(s);
  const half = (DRAWN_ERROR_DEG * Math.PI) / 180;
  const dA = unit(sub(O, A));
  const dB = unit(sub(O, B));
  const rangeA = len(sub(O, A));
  const rangeB = len(sub(O, B));
  const far = 1.3;
  const region = [
    meet(A, rot(dA, -half), B, rot(dB, -half)),
    meet(A, rot(dA, -half), B, rot(dB, half)),
    meet(A, rot(dA, half), B, rot(dB, half)),
    meet(A, rot(dA, half), B, rot(dB, -half)),
  ];
  const parallax = Math.acos(Math.max(-1, Math.min(1, dA[0] * dB[0] + dA[1] * dB[1])));
  // Pinhole camera pointing straight up: u = f·dx/dz, with f = half-width / tan(FOV/2).
  const tanHalf = Math.tan(((FOV_DEG / 2) * Math.PI) / 180);
  return {
    object: O,
    region,
    wedgeA: [add(A, mul(rot(dA, -half), rangeA * far)), add(A, mul(rot(dA, half), rangeA * far))],
    wedgeB: [add(B, mul(rot(dB, -half), rangeB * far)), add(B, mul(rot(dB, half), rangeB * far))],
    parallaxDeg: (parallax * 180) / Math.PI,
    imageA: (O[0] - A[0]) / O[1] / tanHalf,
    imageB: (O[0] - B[0]) / O[1] / tanHalf,
    rangeA,
    rangeB,
  };
}

/** Maps diagram kilometres to SVG user units. */
export const VIEW = {
  width: 640,
  height: 440,
  scale: 30, // px per km
  x0: 140, // x of node A
  ground: 384, // y of the ground
} as const;

export function toSvg(p: P): P {
  return [VIEW.x0 + p[0] * VIEW.scale, VIEW.ground - p[1] * VIEW.scale];
}

export const fmt = (n: number) => n.toFixed(1);
export const pt = (p: P) => {
  const q = toSvg(p);
  return `${fmt(q[0])},${fmt(q[1])}`;
};
