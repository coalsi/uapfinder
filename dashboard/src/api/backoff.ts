export interface BackoffOptions {
  initialMs?: number;
  maxMs?: number;
  factor?: number;
  /** Fractional spread applied to each delay, e.g. 0.2 gives ±20 %. */
  jitter?: number;
  random?: () => number;
}

/** Exponential backoff with symmetric jitter, capped at `maxMs`. */
export class Backoff {
  private readonly initialMs: number;
  private readonly maxMs: number;
  private readonly factor: number;
  private readonly jitter: number;
  private readonly random: () => number;
  private attempts = 0;

  constructor({ initialMs = 1000, maxMs = 30_000, factor = 2, jitter = 0.2, random = Math.random }: BackoffOptions = {}) {
    this.initialMs = initialMs;
    this.maxMs = maxMs;
    this.factor = factor;
    this.jitter = jitter;
    this.random = random;
  }

  get attempt(): number {
    return this.attempts;
  }

  /** Delay before the next attempt; each call doubles (by `factor`) the base delay. */
  next(): number {
    const base = Math.min(this.maxMs, this.initialMs * this.factor ** this.attempts);
    this.attempts += 1;
    const spread = 1 + this.jitter * (2 * this.random() - 1);
    return Math.round(Math.min(this.maxMs, base * spread));
  }

  reset(): void {
    this.attempts = 0;
  }
}
