import { useMemo } from "react";
import { formatAgo, nodeTitle } from "../format";
import type { Selection } from "../selection";
import { useStore } from "../store/store";
import { useNow } from "./useNow";

interface NodeListProps {
  selectedId: string | null;
  onSelect(selection: Selection): void;
}

export function NodeList({ selectedId, onSelect }: NodeListProps) {
  const nodes = useStore((s) => s.nodes);
  const sorted = useMemo(
    () =>
      Object.values(nodes).sort(
        (a, b) => Number(b.online) - Number(a.online) || nodeTitle(a).localeCompare(nodeTitle(b)),
      ),
    [nodes],
  );
  const now = useNow(1000);

  if (sorted.length === 0) return <p className="muted">No nodes registered yet.</p>;
  return (
    <ul className="list nodes" aria-label="Nodes">
      {sorted.map((node) => {
        const active = node.node_id === selectedId;
        return (
          <li key={node.node_id}>
            <button
              type="button"
              className={`row${active ? " active" : ""}`}
              aria-current={active || undefined}
              onClick={() => onSelect({ kind: "node", id: node.node_id })}
            >
              <span className="row-title">
                <span className={`status-dot ${node.online ? "online" : "offline"}`} aria-hidden="true" />
                {nodeTitle(node)}
                <span className="badge">{node.online ? "online" : "offline"}</span>
              </span>
              <span className="row-meta">
                {node.node_id} · seen {formatAgo(node.last_seen_ns, now)}
                {node.status?.fps != null ? ` · ${node.status.fps.toFixed(1)} fps` : ""}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
