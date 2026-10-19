import { useState } from "react";
import { CLASSIFICATIONS, type CoverageStatus } from "../api/types";
import type { LayerVisibility } from "../selection";
import { CLASS_COLORS, CLASS_LABELS, COVERAGE_COLORS, COVERAGE_LABELS, FRUSTUM_COLOR, SUGGESTION_COLOR } from "../theme";

const STATUSES: CoverageStatus[] = ["stereo", "mono", "blind"];

export function Swatch({ color, shape = "square" }: { color: string; shape?: "square" | "line" | "dot" }) {
  return <span className={`swatch swatch-${shape}`} style={{ background: color }} aria-hidden="true" />;
}

/** Collapsible legend for whichever layers are visible. */
export function Legend({ layers }: { layers: LayerVisibility }) {
  // Starts collapsed on phones, where it would cover much of the globe.
  const [initiallyOpen] = useState(() => window.matchMedia("(min-width: 901px)").matches);
  if (!layers.coverage && !layers.tracks && !layers.frustums) return null;
  return (
    <details className="legend" open={initiallyOpen}>
      <summary>Legend</summary>
      {layers.coverage && (
        <ul aria-label="Coverage">
          {STATUSES.map((s) => (
            <li key={s}>
              <Swatch color={COVERAGE_COLORS[s]} />
              {COVERAGE_LABELS[s]}
            </li>
          ))}
          <li>
            <Swatch color={SUGGESTION_COLOR} shape="dot" />
            Suggested node site
          </li>
        </ul>
      )}
      {layers.tracks && (
        <ul aria-label="Track classification">
          {CLASSIFICATIONS.map((c) => (
            <li key={c}>
              <Swatch color={CLASS_COLORS[c]} shape="line" />
              {CLASS_LABELS[c]}
            </li>
          ))}
        </ul>
      )}
      {layers.frustums && (
        <ul aria-label="Cameras">
          <li>
            <Swatch color={FRUSTUM_COLOR} />
            Camera field of view
          </li>
        </ul>
      )}
    </details>
  );
}
