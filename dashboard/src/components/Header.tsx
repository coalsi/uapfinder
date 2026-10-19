import type { ConnectionState } from "../api/live";
import type { Stats } from "../api/types";
import type { Polled } from "../data/hooks";
import { formatLatencyPair, formatRate } from "../format";

type Tone = "good" | "warn" | "bad" | "info";

const CONNECTION: Record<ConnectionState | "demo", { label: string; short: string; tone: Tone }> = {
  live: { label: "Live (WebSocket)", short: "Live", tone: "good" },
  polling: { label: "Live (polling)", short: "Polling", tone: "good" },
  connecting: { label: "Connecting…", short: "Connecting", tone: "warn" },
  offline: { label: "Offline, retrying", short: "Offline", tone: "bad" },
  demo: { label: "Simulated feed", short: "Simulated", tone: "info" },
};

interface HeaderProps {
  demo: boolean;
  connection: ConnectionState | "demo";
  stats: Polled<Stats>;
  siteUrl: string;
}

function Tile({ label, short, value, title }: { label: string; short?: string; value: string; title?: string }) {
  return (
    <div className="tile" title={title}>
      <span className="tile-value">{value}</span>
      <span className="tile-label">
        <span className={short ? "wide-only" : undefined}>{label}</span>
        {short && <span className="narrow-only">{short}</span>}
      </span>
    </div>
  );
}

const DASH = "–";

export function Header({ demo, connection, stats, siteUrl }: HeaderProps) {
  const s = stats.data;
  const conn = CONNECTION[connection];
  const latency = s?.backsignal_latency_ms;
  return (
    <header className="header">
      <div className="brand">
        <span className="brand-name">UAP Finder</span>
        <span className="brand-sub">live map</span>
        {demo && (
          <span className="demo-badge" role="status" title="Simulated nodes and tracks around Phoenix, AZ. Not real observations.">
            DEMO DATA
          </span>
        )}
      </div>
      <div className="tiles" aria-label="Network statistics">
        <Tile label="packets/s" value={s ? formatRate(s.packets_per_s) : DASH} />
        <Tile label="matches/s" value={s ? formatRate(s.matches_per_s) : DASH} />
        <Tile label="active nodes" short="nodes" value={s ? String(s.active_nodes) : DASH} />
        <Tile label="active tracks" short="tracks" value={s ? String(s.active_tracks) : DASH} />
        <Tile
          label="back-signal p50 / p99"
          short="p50 / p99"
          value={latency ? formatLatencyPair(latency.p50, latency.p99) : DASH}
          title="Back-signal latency: server time from a confirmed stereo match to the clip_request hand-off"
        />
      </div>
      <div className={`connection tone-${conn.tone}`} role="status" aria-live="polite">
        <span className="dot" aria-hidden="true" />
        <span className="wide-only">{conn.label}</span>
        <span className="narrow-only">{conn.short}</span>
        {stats.error && (
          <span className="stale" title={stats.error}>
            stats stale
          </span>
        )}
      </div>
      <a className="about-link" href={siteUrl}>
        <span className="wide-only">About UAP Finder</span>
        <span className="narrow-only">About</span>
      </a>
    </header>
  );
}
