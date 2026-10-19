import { describe, expect, it } from "vitest";
import { Backoff } from "./backoff";

describe("Backoff", () => {
  it("doubles from the initial delay up to the cap", () => {
    // random() = 0.5 means no jitter.
    const b = new Backoff({ initialMs: 1000, maxMs: 30_000, random: () => 0.5 });
    const delays = Array.from({ length: 8 }, () => b.next());
    expect(delays).toEqual([1000, 2000, 4000, 8000, 16_000, 30_000, 30_000, 30_000]);
    expect(b.attempt).toBe(8);
  });

  it("spreads each delay by at most ±jitter and never exceeds the cap", () => {
    const low = new Backoff({ initialMs: 1000, maxMs: 30_000, jitter: 0.2, random: () => 0 });
    const high = new Backoff({ initialMs: 1000, maxMs: 30_000, jitter: 0.2, random: () => 0.999999 });
    expect(low.next()).toBe(800);
    expect(high.next()).toBe(1200);
    for (let i = 0; i < 10; i++) {
      expect(high.next()).toBeLessThanOrEqual(30_000);
      expect(low.next()).toBeGreaterThanOrEqual(800);
    }
  });

  it("starts over after reset()", () => {
    const b = new Backoff({ initialMs: 500, factor: 3, random: () => 0.5 });
    expect([b.next(), b.next(), b.next()]).toEqual([500, 1500, 4500]);
    b.reset();
    expect(b.attempt).toBe(0);
    expect(b.next()).toBe(500);
  });
});
