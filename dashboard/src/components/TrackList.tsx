import { useMemo } from "react";
import { formatAccelG, formatAgo, formatAltitude, formatSpeed } from "../format";
import type { Selection } from "../selection";
import { tracksNewestFirst } from "../store/reducer";
import { useStore } from "../store/store";
import { CLASS_COLORS, CLASS_LABELS } from "../theme";
import { Swatch } from "./Legend";
import { useNow } from "./useNow";

interface TrackListProps {
  selectedId: string | null;
  onSelect(selection: Selection): void;
}

export function TrackList({ selectedId, onSelect }: TrackListProps) {
  const tracks = useStore((s) => s.tracks);
  const sorted = useMemo(() => tracksNewestFirst(tracks), [tracks]);
  const now = useNow(1000);

  if (sorted.length === 0) return <p className="muted">No tracks yet. Triangulated objects appear here live.</p>;
  return (
    <ol className="list tracks" aria-label="Tracks, newest first">
      {sorted.map(({ track }) => {
        const active = track.track_id === selectedId;
        return (
          <li key={track.track_id}>
            <button
              type="button"
              className={`row${active ? " active" : ""}`}
              aria-current={active || undefined}
              onClick={() => onSelect({ kind: "track", id: track.track_id })}
            >
              <span className="row-title">
                <Swatch color={CLASS_COLORS[track.classification]} shape="line" />
                {track.track_id}
                <span className={`badge class-${track.classification}`}>{CLASS_LABELS[track.classification]}</span>
              </span>
              <span className="row-grid">
                <span>
                  <b>{formatSpeed(track.speed_mps)}</b> now
                </span>
                <span>
                  <b>{formatSpeed(track.max_speed_mps)}</b> max
                </span>
                <span>
                  <b>{formatAccelG(track.max_accel_mps2)}</b> max
                </span>
                <span>
                  <b>{track.node_ids.length}</b> nodes
                </span>
              </span>
              <span className="row-meta">
                {track.last_point ? `${formatAltitude(track.last_point.alt_m)} · ` : ""}
                {formatAgo(track.last_ns, now)}
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}
