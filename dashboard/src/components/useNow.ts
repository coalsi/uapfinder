import { useEffect, useState } from "react";

/** Wall-clock time (ms) that re-renders every `intervalMs`, for "x s ago" labels. */
export function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}
