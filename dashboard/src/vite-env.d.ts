interface ImportMetaEnv {
  /** API server root, e.g. https://uapfinder.example.ts.net. Empty: same origin as the page. */
  readonly VITE_API_BASE?: string;
  /** Public base path the dashboard is served from (build time), e.g. "/map/". Default "/". */
  readonly VITE_BASE_PATH?: string;
  /** Public website the "About UAP Finder" link points to. Default "/". */
  readonly VITE_SITE_URL?: string;
  /** "1" starts in demo mode (client-side simulator). */
  readonly VITE_DEMO?: string;
  /** Cesium ion token; enables Cesium World Terrain and imagery. */
  readonly VITE_CESIUM_ION_TOKEN?: string;
  /** XYZ tile URL template for the base map ({z}/{x}/{y}); defaults to OpenStreetMap. */
  readonly VITE_IMAGERY_URL?: string;
  /** HTML attribution for VITE_IMAGERY_URL. */
  readonly VITE_IMAGERY_CREDIT?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

interface Window {
  /** Where CesiumJS loads its Workers, Assets, Widgets and ThirdParty files from (see src/cesiumBase.ts). */
  CESIUM_BASE_URL?: string;
}
