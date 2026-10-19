import { Suspense, lazy, useCallback, useRef, useState } from "react";
import type { AppConfig, PanelTab } from "./config";
import { Header } from "./components/Header";
import { LayerBar } from "./components/LayerBar";
import { Legend } from "./components/Legend";
import { Panel } from "./components/Panel";
import type { Backend } from "./data/backend";
import { useCoverage, useLiveFeed, useStats } from "./data/hooks";
import type { GeoRect } from "./geo/coverageQuery";
import type { GlobeHandle } from "./globe/GlobeView";
import type { LayerVisibility, Selection } from "./selection";
import { type Store, StoreContext } from "./store/store";

const GlobeView = lazy(() => import("./globe/GlobeView"));

export const DEFAULT_REF_ALT_M = 10_000;
const DEFAULT_LAYERS: LayerVisibility = { frustums: true, tracks: true, coverage: true };

const TAB_FOR: Record<Selection["kind"], PanelTab> = { node: "nodes", track: "tracks", suggestion: "coverage" };

interface AppProps {
  config: AppConfig;
  backend: Backend;
  store: Store;
}

export default function App({ config, backend, store }: AppProps) {
  const connection = useLiveFeed(backend, store);
  const stats = useStats(backend);
  const [layers, setLayers] = useState(DEFAULT_LAYERS);
  const [frustumRangeKm, setFrustumRangeKm] = useState(25);
  const [refAltM, setRefAltM] = useState(DEFAULT_REF_ALT_M);
  const [view, setView] = useState<GeoRect | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  // `?layer=` (the website's "where nodes are needed" link) opens that panel, also on phones.
  const [tab, setTab] = useState<PanelTab>(config.initialPanel ?? "coverage");
  const [drawerOpen, setDrawerOpen] = useState(config.initialPanel !== null);
  const coverage = useCoverage(backend, view, refAltM, layers.coverage);
  const globe = useRef<GlobeHandle>(null);

  const select = useCallback((next: Selection | null, fly: boolean) => {
    setSelection(next);
    if (!next) return;
    setTab(TAB_FOR[next.kind]);
    setDrawerOpen(true);
    if (fly) globe.current?.flyTo(next);
  }, []);
  const onGlobeSelect = useCallback((next: Selection | null) => select(next, false), [select]);
  const onPanelSelect = useCallback((next: Selection) => select(next, true), [select]);

  return (
    <StoreContext.Provider value={store}>
      <div className="app">
        <Header demo={backend.kind === "demo"} connection={connection} stats={stats} siteUrl={config.siteUrl} />
        <main className="stage">
          <Suspense fallback={<div className="globe globe-loading">Loading 3D globe…</div>}>
            <GlobeView
              ref={globe}
              config={config}
              coverage={coverage.coverage}
              coverageHeightM={coverage.query?.ref_alt_m ?? refAltM}
              layers={layers}
              frustumRangeKm={frustumRangeKm}
              selection={selection}
              onSelect={onGlobeSelect}
              onViewChange={setView}
            />
          </Suspense>
          <LayerBar
            layers={layers}
            onLayersChange={setLayers}
            frustumRangeKm={frustumRangeKm}
            onFrustumRangeChange={setFrustumRangeKm}
          />
          <Legend layers={layers} />
          {backend.kind === "demo" && (
            <p className="demo-watermark" aria-hidden="true">
              DEMO DATA · simulated network near Phoenix, AZ
            </p>
          )}
        </main>
        <Panel
          backend={backend}
          tab={tab}
          onTabChange={setTab}
          open={drawerOpen}
          onOpenChange={setDrawerOpen}
          selection={selection}
          onSelect={onPanelSelect}
          onClearSelection={() => setSelection(null)}
          coverage={coverage}
          coverageEnabled={layers.coverage}
          onEnableCoverage={() => setLayers((l) => ({ ...l, coverage: true }))}
          refAltM={refAltM}
          onRefAltChange={setRefAltM}
          hostUrl={config.hostUrl}
        />
      </div>
    </StoreContext.Provider>
  );
}
