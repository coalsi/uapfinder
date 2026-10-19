import type { LayerVisibility } from "../selection";

const RANGES_KM = [5, 10, 25, 50, 80] as const;

interface LayerBarProps {
  layers: LayerVisibility;
  onLayersChange(layers: LayerVisibility): void;
  frustumRangeKm: number;
  onFrustumRangeChange(km: number): void;
}

const LABELS: Record<keyof LayerVisibility, string> = {
  coverage: "Coverage",
  frustums: "Camera views",
  tracks: "Tracks",
};

/** Floating layer toggles over the globe. */
export function LayerBar({ layers, onLayersChange, frustumRangeKm, onFrustumRangeChange }: LayerBarProps) {
  return (
    <div className="layer-bar" role="toolbar" aria-label="Map layers">
      {(Object.keys(LABELS) as (keyof LayerVisibility)[]).map((key) => (
        <button
          key={key}
          type="button"
          className="chip"
          aria-pressed={layers[key]}
          onClick={() => onLayersChange({ ...layers, [key]: !layers[key] })}
        >
          {LABELS[key]}
        </button>
      ))}
      <label className="chip chip-select">
        <span>View range</span>
        <select
          value={frustumRangeKm}
          onChange={(e) => onFrustumRangeChange(Number(e.target.value))}
          disabled={!layers.frustums}
        >
          {RANGES_KM.map((km) => (
            <option key={km} value={km}>
              {km} km
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}
