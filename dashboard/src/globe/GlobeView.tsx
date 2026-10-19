import { type Ref, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState } from "react";
import type { Coverage } from "../api/types";
import type { AppConfig } from "../config";
import type { GeoRect } from "../geo/coverageQuery";
import type { LayerVisibility, Selection } from "../selection";
import { useStoreApi } from "../store/store";
import { GlobeController } from "./GlobeController";

export interface GlobeHandle {
  flyTo(target: Selection): void;
}

export interface GlobeViewProps {
  config: AppConfig;
  coverage: Coverage | null;
  coverageHeightM: number;
  layers: LayerVisibility;
  frustumRangeKm: number;
  selection: Selection | null;
  onSelect(selection: Selection | null): void;
  onViewChange(view: GeoRect | null): void;
  ref?: Ref<GlobeHandle>;
}

/** React shell around {@link GlobeController}; loaded lazily so the UI renders before the CesiumJS chunk arrives. */
export default function GlobeView({ ref, ...props }: GlobeViewProps) {
  const store = useStoreApi();
  const container = useRef<HTMLDivElement>(null);
  const controller = useRef<GlobeController | null>(null);
  const latest = useRef(props);
  const [error, setError] = useState<string | null>(null);

  useLayoutEffect(() => {
    latest.current = props;
  });

  const push = () => {
    const p = latest.current;
    controller.current?.update({
      state: store.getState(),
      coverage: p.coverage,
      coverageHeightM: p.coverageHeightM,
      layers: p.layers,
      frustumRangeKm: p.frustumRangeKm,
      selection: p.selection,
    });
  };

  useEffect(() => {
    if (!container.current) return;
    let instance: GlobeController;
    try {
      instance = new GlobeController(container.current, props.config, {
        onSelect: (s) => latest.current.onSelect(s),
        onViewChange: (v) => latest.current.onViewChange(v),
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return;
    }
    controller.current = instance;
    push();
    const unsubscribe = store.subscribe(push);
    return () => {
      unsubscribe();
      controller.current = null;
      instance.destroy();
    };
    // `push` only reads refs; the viewer is recreated only for a new config or store.
  }, [props.config, store]);

  useEffect(push, [props.coverage, props.coverageHeightM, props.layers, props.frustumRangeKm, props.selection]);

  useImperativeHandle(ref, () => ({ flyTo: (target) => controller.current?.flyTo(target) }), []);

  return (
    <div className="globe" ref={container} role="application" aria-label="3D globe of nodes, tracks and coverage">
      {error && (
        <p className="globe-error" role="alert">
          The 3D globe could not start (WebGL is required): {error}. Panels and lists still update.
        </p>
      )}
    </div>
  );
}
