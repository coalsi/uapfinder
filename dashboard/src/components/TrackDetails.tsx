import { useEffect, useMemo, useState } from "react";
import type { Backend } from "../data/backend";
import { formatAccelG, formatAgo, formatAltitude, formatMeters, formatSpeed, formatTime } from "../format";
import { useStore, useStoreApi } from "../store/store";
import { CLASS_LABELS } from "../theme";
import { useNow } from "./useNow";

const DETAIL_REFRESH_MS = 10_000;

interface TrackDetailsProps {
  backend: Backend;
  trackId: string;
  onClose(): void;
}

/** Loads GET /tracks/{id} (all points + clips) into the store while the track is selected. */
function useTrackDetail(backend: Backend, trackId: string): string | null {
  const store = useStoreApi();
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const detail = await backend.getTrack(trackId);
        if (cancelled) return;
        store.dispatch({ type: "trackDetail", detail });
        setError(null);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
      if (!cancelled) timer = setTimeout(() => void load(), DETAIL_REFRESH_MS);
    };
    void load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [backend, store, trackId]);
  return error;
}

export function TrackDetails({ backend, trackId, onClose }: TrackDetailsProps) {
  const entry = useStore((s) => s.tracks[trackId]);
  const clipMap = useStore((s) => s.clips[trackId]);
  const clips = useMemo(() => Object.values(clipMap ?? {}), [clipMap]);
  const error = useTrackDetail(backend, trackId);
  const now = useNow(1000);
  if (!entry) return <p className="muted">Track {trackId} is no longer in memory.</p>;
  const { track } = entry;
  const p = track.last_point;

  return (
    <article className="details" aria-labelledby="track-details-title">
      <header className="details-header">
        <h2 id="track-details-title">
          {track.track_id}
          <span className={`badge class-${track.classification}`}>{CLASS_LABELS[track.classification]}</span>
        </h2>
        <button type="button" className="close" onClick={onClose} aria-label="Close track details">
          ×
        </button>
      </header>
      {track.flags.length > 0 && (
        <p className="flags">
          Rules fired:{" "}
          {track.flags.map((f) => (
            <span key={f} className="badge">
              {f}
            </span>
          ))}
        </p>
      )}
      <dl className="facts">
        <dt>Speed</dt>
        <dd>
          {formatSpeed(track.speed_mps)} (max {formatSpeed(track.max_speed_mps)})
        </dd>
        <dt>Max acceleration</dt>
        <dd>
          {formatAccelG(track.max_accel_mps2)} ({track.max_accel_mps2.toFixed(0)} m/s²)
        </dd>
        <dt>Nodes</dt>
        <dd className="mono">{track.node_ids.join(", ")}</dd>
        <dt>Points</dt>
        <dd>
          {track.point_count.toLocaleString("en-US")}
          {entry.points.length < track.point_count && ` (${entry.points.length} drawn)`}
        </dd>
        <dt>First seen</dt>
        <dd>{formatTime(track.first_ns)}</dd>
        <dt>Last seen</dt>
        <dd title={formatTime(track.last_ns)}>{formatAgo(track.last_ns, now)}</dd>
        {p && (
          <>
            <dt>Latest fix</dt>
            <dd>
              {p.lat.toFixed(4)}, {p.lon.toFixed(4)}, {formatAltitude(p.alt_m)} HAE
              <span className="block muted small">
                σ {formatMeters(p.sigma_m)} · ray miss {formatMeters(p.error_margin_m)}
              </span>
            </dd>
          </>
        )}
      </dl>
      <h3>Video clips</h3>
      {error && <p className="error small">Could not load track details: {error}</p>}
      {clips.length === 0 ? (
        <p className="muted small">No clips requested yet.</p>
      ) : (
        <ul className="clips">
          {clips.map((c) => (
            <li key={c.request_id}>
              <span className="mono">{c.node_id}</span>
              <span className={`badge clip-${c.status}`}>{c.status}</span>
              {c.status === "uploaded" && c.download_url && (
                <a href={c.download_url} download target="_blank" rel="noopener">
                  Download
                </a>
              )}
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}
