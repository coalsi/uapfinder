import { useMemo } from "react";
import type { PanelTab } from "../config";
import type { Backend } from "../data/backend";
import type { CoverageResult } from "../data/hooks";
import type { Selection } from "../selection";
import { useStore } from "../store/store";
import { CoveragePanel } from "./CoveragePanel";
import { NodeDetails } from "./NodeDetails";
import { NodeList } from "./NodeList";
import { TrackDetails } from "./TrackDetails";
import { TrackList } from "./TrackList";


interface PanelProps {
  backend: Backend;
  tab: PanelTab;
  onTabChange(tab: PanelTab): void;
  /** Drawer state on narrow screens; the desktop sidebar is always open. */
  open: boolean;
  onOpenChange(open: boolean): void;
  selection: Selection | null;
  onSelect(selection: Selection): void;
  onClearSelection(): void;
  coverage: CoverageResult;
  coverageEnabled: boolean;
  onEnableCoverage(): void;
  refAltM: number;
  onRefAltChange(m: number): void;
  hostUrl: string;
}

export function Panel(props: PanelProps) {
  const { tab, open, selection } = props;
  const nodes = useStore((s) => s.nodes);
  const tracks = useStore((s) => s.tracks);
  const counts = useMemo(() => {
    const list = Object.values(nodes);
    return { online: list.filter((n) => n.online).length, nodes: list.length, tracks: Object.keys(tracks).length };
  }, [nodes, tracks]);

  const tabs: { id: PanelTab; label: string }[] = [
    { id: "coverage", label: "Coverage" },
    { id: "tracks", label: `Tracks (${counts.tracks})` },
    { id: "nodes", label: `Nodes (${counts.online}/${counts.nodes})` },
  ];

  const choose = (id: PanelTab) => {
    if (id === tab) props.onOpenChange(!open);
    else {
      props.onTabChange(id);
      props.onOpenChange(true);
    }
  };

  return (
    <aside className={`panel${open ? " open" : ""}`} aria-label="Details">
      <div className="tabs" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.id}
            id={`tab-${t.id}`}
            type="button"
            role="tab"
            aria-selected={t.id === tab}
            aria-controls="panel-body"
            className="tab"
            onClick={() => choose(t.id)}
          >
            {t.label}
          </button>
        ))}
        <button
          type="button"
          className="drawer-toggle"
          aria-expanded={open}
          aria-controls="panel-body"
          aria-label={open ? "Collapse panel" : "Expand panel"}
          onClick={() => props.onOpenChange(!open)}
        >
          {open ? "▾" : "▴"}
        </button>
      </div>
      <div className="panel-body" id="panel-body" role="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === "coverage" && (
          <CoveragePanel
            result={props.coverage}
            enabled={props.coverageEnabled}
            onEnable={props.onEnableCoverage}
            refAltM={props.refAltM}
            onRefAltChange={props.onRefAltChange}
            selection={selection?.kind === "suggestion" ? selection : null}
            onSelect={props.onSelect}
            hostUrl={props.hostUrl}
          />
        )}
        {tab === "tracks" && (
          <>
            {selection?.kind === "track" && (
              <TrackDetails backend={props.backend} trackId={selection.id} onClose={props.onClearSelection} />
            )}
            <TrackList selectedId={selection?.kind === "track" ? selection.id : null} onSelect={props.onSelect} />
          </>
        )}
        {tab === "nodes" && (
          <>
            {selection?.kind === "node" && <NodeDetails nodeId={selection.id} onClose={props.onClearSelection} />}
            <NodeList selectedId={selection?.kind === "node" ? selection.id : null} onSelect={props.onSelect} />
          </>
        )}
      </div>
    </aside>
  );
}
