import type { ConnectionState } from "../api/live";
import type { Stats } from "../api/types";
import type { Polled } from "../data/hooks";
import { formatLatencyMs, formatRate } from "../format";

const CONNECTION: Record<ConnectionState | "demo", { label: string; tone: "good" | "warn" | "bad" | "info" }> = {
  live: { label: "Live (WebSocket)", tone: "good" },
  polling: { label: "Live (polling)", tone: "good" },
  connecting: { label: "Connecting…", tone: "warn" },
  offline: { label: "Offline, retrying", tone: "bad" },
  demo: { label: "Simulated feed", tone: "info" },
};

interface HeaderProps {
  demo: boolean;
  connection: ConnectionState | "demo";
  stats: Polled<Stats>;
}

function Tile({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="tile" title={title}>
      <span className="tile-value">{value}</span>
      <span className="tile-label">{label}</span>
    </div>
  );
}

export function Header({ demo, connection, stats }: HeaderProps) {
  const s = stats.data;
  const dash = "–";
  const conn = CONNECTION[connection];
  return (
    <header className="header">
      <div className="brand">
        <span className="brand-name">SKY-NET</span>
        <span className="brand-sub">stereo UAP network</span>
        {demo && (
          <span className="demo-badge" role="status">
            DEMO DATA
          </span>
        )}
      </div>
      <div className="tiles" aria-label="Network statistics">
        <Tile label="packets/s" value={s ? formatRate(s.packets_per_s) : dash} />
        <Tile label="matches/s" value={s ? formatRate(s.matches_per_s) : dash} />
        <Tile label="active nodes" value={s ? String(s.active_nodes) : dash} />
        <Tile label="active tracks" value={s ? String(s.active_tracks) : dash} />
        <Tile
          label="back-signal p50 / p99"
          value={s ? `${formatLatencyMs(s.backsignal_latency_ms.p50)} / ${formatLatencyMs(s.backsignal_latency_ms.p99)}` : dash}
          title="Server time from stereo match to clip_request hand-off"
        />
      </div>
      <div className={`connection tone-${conn.tone}`} role="status" aria-live="polite">
        <span className="dot" aria-hidden="true" />
        <span>{conn.label}</span>
        {stats.error && <span className="stale" title={stats.error}>stats stale</span>}
      </div>
    </header>
  );
}
