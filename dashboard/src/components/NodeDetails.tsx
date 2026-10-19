import { formatAgo, formatAltitude, formatTime, nodeTitle } from "../format";
import { useStore } from "../store/store";
import { useNow } from "./useNow";

const deg = (v: number | null) => (v == null ? "–" : `${v.toFixed(1)}°`);

interface NodeDetailsProps {
  nodeId: string;
  onClose(): void;
}

export function NodeDetails({ nodeId, onClose }: NodeDetailsProps) {
  const node = useStore((s) => s.nodes[nodeId]);
  const now = useNow(1000);
  if (!node) return null;
  const status = node.status ?? {};
  const hasCamera = node.yaw_deg != null && node.fov_h_deg != null;

  return (
    <article className="details" aria-labelledby="node-details-title">
      <header className="details-header">
        <h2 id="node-details-title">
          <span className={`status-dot ${node.online ? "online" : "offline"}`} aria-hidden="true" />
          {nodeTitle(node)}
        </h2>
        <button type="button" className="close" onClick={onClose} aria-label="Close node details">
          ×
        </button>
      </header>
      <dl className="facts">
        <dt>Status</dt>
        <dd>{node.online ? "Online" : "Offline"}</dd>
        <dt>Last seen</dt>
        <dd title={node.last_seen_ns ? formatTime(node.last_seen_ns) : undefined}>{formatAgo(node.last_seen_ns, now)}</dd>
        <dt>Frame rate</dt>
        <dd>{typeof status.fps === "number" ? `${status.fps.toFixed(1)} fps` : "–"}</dd>
        <dt>Memory used</dt>
        <dd>{typeof status.mem_used_mb === "number" ? `${status.mem_used_mb.toLocaleString("en-US")} MB` : "–"}</dd>
        {typeof status.gpu_temp_c === "number" && (
          <>
            <dt>GPU temperature</dt>
            <dd>{status.gpu_temp_c.toFixed(1)} °C</dd>
          </>
        )}
        <dt>Software</dt>
        <dd>{node.software_version ?? "–"}</dd>
        <dt>Node id</dt>
        <dd className="mono">{node.node_id}</dd>
        <dt>Position</dt>
        <dd>
          {node.lat != null && node.lon != null ? (
            <>
              ≈ {node.lat.toFixed(2)}, {node.lon.toFixed(2)}
              {node.alt_m != null && `, ${formatAltitude(node.alt_m)} HAE`}
              <span className="muted small block">Public positions are rounded to about 1 km.</span>
            </>
          ) : (
            "not reported yet"
          )}
        </dd>
        <dt>Camera</dt>
        <dd>
          {hasCamera ? (
            <>
              az {deg(node.yaw_deg)} · el {deg(node.pitch_deg)} · roll {deg(node.roll_deg)}
              <span className="block">
                FOV {deg(node.fov_h_deg)} × {deg(node.fov_v_deg)}
              </span>
            </>
          ) : (
            "not reported yet (needs a heartbeat)"
          )}
        </dd>
      </dl>
    </article>
  );
}
