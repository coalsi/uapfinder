import type { CoverageStatus } from "../api/types";
import type { CoverageResult } from "../data/hooks";
import { formatAltitude } from "../format";
import type { Selection } from "../selection";
import { COVERAGE_COLORS, COVERAGE_LABELS, SUGGESTION_COLOR } from "../theme";
import { Swatch } from "./Legend";

export const REF_ALTITUDES_M = [1000, 3000, 10_000, 20_000] as const;
const STATUSES: CoverageStatus[] = ["stereo", "mono", "blind"];

interface CoveragePanelProps {
  result: CoverageResult;
  enabled: boolean;
  onEnable(): void;
  refAltM: number;
  onRefAltChange(m: number): void;
  selection: Extract<Selection, { kind: "suggestion" }> | null;
  onSelect(selection: Selection): void;
  hostUrl: string;
}

export function CoveragePanel(props: CoveragePanelProps) {
  const { result, enabled, onEnable, refAltM, onRefAltChange, selection, onSelect, hostUrl } = props;
  const { coverage, query, loading, error } = result;
  const total = coverage ? coverage.summary.blind + coverage.summary.mono + coverage.summary.stereo : 0;

  return (
    <section className="coverage" aria-labelledby="coverage-title">
      <h2 id="coverage-title">Where the network is blind</h2>
      <p className="lede">
        An object is only located in 3D when two cameras see it from different angles. Red cells have no camera, amber
        cells only one. A node at a suggested site turns the most cells green: rural sites far from existing nodes
        matter most.
      </p>

      {!enabled && (
        <button type="button" className="button primary" onClick={onEnable}>
          Show coverage map
        </button>
      )}

      <fieldset className="segmented">
        <legend>Altitude slice</legend>
        {REF_ALTITUDES_M.map((m) => (
          <label key={m}>
            <input type="radio" name="ref-alt" value={m} checked={m === refAltM} onChange={() => onRefAltChange(m)} />
            <span>{formatAltitude(m)}</span>
          </label>
        ))}
      </fieldset>

      <div className="summary" aria-live="polite">
        {STATUSES.map((s) => (
          <div key={s} className="summary-tile">
            <span className="summary-label">
              <Swatch color={COVERAGE_COLORS[s]} />
              {COVERAGE_LABELS[s]}
            </span>
            <span className="summary-value">{coverage ? coverage.summary[s].toLocaleString("en-US") : "–"}</span>
            <span className="summary-share">
              {coverage && total > 0 ? `${Math.round((100 * coverage.summary[s]) / total)} % of cells` : " "}
            </span>
          </div>
        ))}
      </div>
      <p className="muted small">
        {loading && "Updating… "}
        {error && <span className="error">Coverage unavailable: {error}. </span>}
        {query
          ? `Visible area, ${query.cell_km} km cells at ${formatAltitude(query.ref_alt_m)} above the ellipsoid.`
          : enabled && "Coverage loads for the area in view."}
      </p>

      <aside className="cta" aria-label="Host a node">
        <p>
          <strong>Live under a red or amber patch?</strong> One rooftop or backyard camera there turns the cells it
          shares with a neighbour green. Rural sites far from existing nodes help most.
        </p>
        <a className="button primary" href={hostUrl}>
          Host a node
        </a>
      </aside>

      <h3>
        <Swatch color={SUGGESTION_COLOR} shape="dot" /> Deploy a node here
      </h3>
      {coverage && coverage.suggestions.length === 0 && (
        <p className="muted small">No site in view would add stereo cells at this altitude. Try another slice or pan out.</p>
      )}
      <ol className="list suggestions">
        {coverage?.suggestions.map((s, i) => {
          const active = selection?.lat === s.lat && selection.lon === s.lon;
          return (
            <li key={`${s.lat},${s.lon}`}>
              <button
                type="button"
                className={`row${active ? " active" : ""}`}
                aria-current={active || undefined}
                onClick={() => onSelect({ kind: "suggestion", lat: s.lat, lon: s.lon, gain: s.gain_cells })}
              >
                <span className="row-title">
                  #{i + 1} · +{s.gain_cells} stereo cells
                </span>
                <span className="row-meta">
                  {s.lat.toFixed(3)}, {s.lon.toFixed(3)} · zenith camera, 90° FOV
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
