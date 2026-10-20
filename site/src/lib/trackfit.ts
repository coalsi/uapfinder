/**
 * Synthetic fixes and least-squares fits for the "from fixes to a track"
 * illustration on /device/. Pure and deterministic (seeded noise), so the
 * diagram is reproducible and the numbers it prints are the real outputs of
 * the fits, not hand-picked.
 */

/** Small deterministic PRNG (mulberry32). */
export function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function gaussian(next: () => number): () => number {
  return () => {
    const u = Math.max(next(), 1e-12);
    const v = next();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  };
}

export interface Fix {
  t: number;
  x: number;
  z: number;
}

/** Fit y = c0 + c1·t + c2·t² (degree 1 or 2) and return coefficients with their standard errors for noise sigma. */
export function polyfit(ts: number[], ys: number[], degree: 1 | 2, sigma: number): { c: number[]; se: number[] } {
  const n = degree + 1;
  const M = Array.from({ length: n }, () => new Array<number>(n).fill(0));
  const v = new Array<number>(n).fill(0);
  ts.forEach((t, i) => {
    const row = Array.from({ length: n }, (_, k) => t ** k);
    for (let r = 0; r < n; r++) {
      v[r]! += row[r]! * ys[i]!;
      for (let c = 0; c < n; c++) M[r]![c]! += row[r]! * row[c]!;
    }
  });
  const inv = invert(M);
  const c = inv.map((row) => row.reduce((s, m, k) => s + m * v[k]!, 0));
  const se = inv.map((row, k) => sigma * Math.sqrt(row[k]!));
  return { c, se };
}

function invert(m: number[][]): number[][] {
  const n = m.length;
  const a = m.map((row, i) => [...row, ...Array.from({ length: n }, (_, k) => (k === i ? 1 : 0))]);
  for (let col = 0; col < n; col++) {
    let piv = col;
    for (let r = col + 1; r < n; r++) if (Math.abs(a[r]![col]!) > Math.abs(a[piv]![col]!)) piv = r;
    [a[col], a[piv]] = [a[piv]!, a[col]!];
    const p = a[col]![col]!;
    for (let k = 0; k < 2 * n; k++) a[col]![k]! /= p;
    for (let r = 0; r < n; r++) {
      if (r === col) continue;
      const f = a[r]![col]!;
      for (let k = 0; k < 2 * n; k++) a[r]![k]! -= f * a[col]![k]!;
    }
  }
  return a.map((row) => row.slice(n));
}

export const TRACK = {
  seconds: 3,
  hz: 10,
  sigma: 12, // metres per axis, typical of a fix at 0.05° and ~10 km range (docs/MATH.md)
  vx: 200, // m/s
  vz: 20,
  az: 12, // m/s²
} as const;

export function simulate(seed = 7): Fix[] {
  const g = gaussian(rng(seed));
  const n = TRACK.seconds * TRACK.hz + 1;
  return Array.from({ length: n }, (_, i) => {
    const t = i / TRACK.hz;
    return {
      t,
      x: TRACK.vx * t + TRACK.sigma * g(),
      z: TRACK.vz * t + 0.5 * TRACK.az * t * t + TRACK.sigma * g(),
    };
  });
}

/** Speed from each pair of consecutive fixes: what you get without fitting. */
export function pointToPointSpeeds(fixes: Fix[]): { t: number; v: number }[] {
  return fixes.slice(1).map((f, i) => {
    const p = fixes[i]!;
    return { t: (f.t + p.t) / 2, v: Math.hypot(f.x - p.x, f.z - p.z) / (f.t - p.t) };
  });
}

/** Speed from a straight-line fit to the last `window` seconds of fixes, with its standard error. */
export function slidingFitSpeeds(fixes: Fix[], window = 1, step = 0.5): { t: number; v: number; se: number }[] {
  const out: { t: number; v: number; se: number }[] = [];
  for (let end = window; end <= TRACK.seconds + 1e-9; end += step) {
    const sel = fixes.filter((f) => f.t >= end - window - 1e-9 && f.t <= end + 1e-9);
    const ts = sel.map((f) => f.t - end);
    const fx = polyfit(ts, sel.map((f) => f.x), 1, TRACK.sigma);
    const fz = polyfit(ts, sel.map((f) => f.z), 1, TRACK.sigma);
    const vx = fx.c[1]!;
    const vz = fz.c[1]!;
    const v = Math.hypot(vx, vz);
    // Propagate both components' standard errors onto the speed.
    const se = Math.hypot((vx / v) * fx.se[1]!, (vz / v) * fz.se[1]!);
    out.push({ t: end, v, se });
  }
  return out;
}

/** Parabola fit to the whole track: speed and acceleration at the last fix, with standard errors. */
export function wholeTrackFit(fixes: Fix[]) {
  const tEnd = fixes[fixes.length - 1]!.t;
  const ts = fixes.map((f) => f.t - tEnd);
  const fx = polyfit(ts, fixes.map((f) => f.x), 2, TRACK.sigma);
  const fz = polyfit(ts, fixes.map((f) => f.z), 2, TRACK.sigma);
  const vx = fx.c[1]!;
  const vz = fz.c[1]!;
  const ax = 2 * fx.c[2]!;
  const az = 2 * fz.c[2]!;
  const v = Math.hypot(vx, vz);
  const a = Math.hypot(ax, az);
  return {
    curve: (t: number) => {
      const s = t - tEnd;
      return { x: fx.c[0]! + fx.c[1]! * s + fx.c[2]! * s * s, z: fz.c[0]! + fz.c[1]! * s + fz.c[2]! * s * s };
    },
    v,
    vSe: Math.hypot((vx / v) * fx.se[1]!, (vz / v) * fz.se[1]!),
    a,
    aSe: Math.hypot((ax / a) * 2 * fx.se[2]!, (az / a) * 2 * fz.se[2]!),
  };
}
