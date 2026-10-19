import type { Node } from "./api/types";

export const STANDARD_GRAVITY = 9.80665;

const num = (value: number, digits: number) =>
  value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });

export function formatSpeed(mps: number): string {
  return mps >= 1000 ? `${num(mps / 1000, 2)} km/s` : `${num(mps, 0)} m/s`;
}

export function formatAccelG(mps2: number): string {
  return `${num(mps2 / STANDARD_GRAVITY, 1)} g`;
}

export function formatAltitude(m: number): string {
  return m >= 1000 ? `${num(m / 1000, 1)} km` : `${num(m, 0)} m`;
}

export function formatMeters(m: number): string {
  return m >= 1000 ? `${num(m / 1000, 2)} km` : `${num(m, 1)} m`;
}

export function formatRate(perSecond: number): string {
  return perSecond >= 1000 ? `${num(perSecond / 1000, 1)}k` : num(perSecond, perSecond < 10 ? 1 : 0);
}

export function formatLatencyMs(ms: number): string {
  return ms < 10 ? `${num(ms, 2)} ms` : `${num(ms, 0)} ms`;
}

/** "12 s ago" style age of a nanosecond timestamp. */
export function formatAgo(ns: number | null, nowMs: number): string {
  if (ns === null) return "never";
  const s = Math.max(0, (nowMs - ns / 1e6) / 1000);
  if (s < 60) return `${Math.floor(s)} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86_400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86_400)} d ago`;
}

export function formatTime(ns: number): string {
  return new Date(ns / 1e6).toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");
}

export function nodeTitle(node: Node): string {
  return node.label ?? node.node_id;
}
