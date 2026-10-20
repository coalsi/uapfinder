import { describe, expect, it } from "vitest";
import { pairGeometry, objectAt, BASELINE_KM } from "../../src/lib/stereo";
import { polyfit, simulate, slidingFitSpeeds, pointToPointSpeeds, wholeTrackFit, TRACK } from "../../src/lib/trackfit";

describe("stereo pair geometry", () => {
  it("keeps the object inside both 90° fields of view for the whole loop", () => {
    for (let s = 0; s < 1; s += 0.01) {
      const g = pairGeometry(s);
      expect(Math.abs(g.imageA)).toBeLessThan(1);
      expect(Math.abs(g.imageB)).toBeLessThan(1);
    }
  });

  it("gives the textbook stereo disparity f·B/Z for cameras pointing the same way", () => {
    const g = pairGeometry(0.3);
    const z = objectAt(0.3)[1];
    // image coordinates are normalised to the half-width, i.e. f = 1 for a 90° lens
    expect(g.imageA - g.imageB).toBeCloseTo(BASELINE_KM / z, 9);
  });

  it("puts the error region around the object", () => {
    const g = pairGeometry(0.6);
    const cx = g.region.reduce((s, p) => s + p[0], 0) / 4;
    const cz = g.region.reduce((s, p) => s + p[1], 0) / 4;
    expect(Math.hypot(cx - g.object[0], cz - g.object[1])).toBeLessThan(0.2);
  });
});

describe("track fits", () => {
  it("recovers an exact polynomial", () => {
    const ts = [0, 0.5, 1, 1.5, 2];
    const { c } = polyfit(ts, ts.map((t) => 3 + 2 * t + 0.5 * t * t), 2, 1);
    expect(c[0]).toBeCloseTo(3, 9);
    expect(c[1]).toBeCloseTo(2, 9);
    expect(c[2]).toBeCloseTo(0.5, 9);
  });

  it("fitted speeds are far less noisy than point-to-point differences", () => {
    const fixes = simulate();
    const truth = (t: number) => Math.hypot(TRACK.vx, TRACK.vz + TRACK.az * t);
    const rms = (xs: { t: number; v: number }[]) => Math.sqrt(xs.reduce((s, p) => s + (p.v - truth(p.t)) ** 2, 0) / xs.length);
    expect(rms(slidingFitSpeeds(fixes))).toBeLessThan(rms(pointToPointSpeeds(fixes)) / 2);
    const whole = wholeTrackFit(fixes);
    expect(Math.abs(whole.v - truth(TRACK.seconds))).toBeLessThan(4 * whole.vSe);
    expect(Math.abs(whole.a - TRACK.az)).toBeLessThan(4 * whole.aSe);
  });
});
