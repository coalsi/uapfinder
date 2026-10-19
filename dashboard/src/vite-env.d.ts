interface ImportMetaEnv {
  /** API server root, e.g. https://skynet.example.org. Empty: same origin. */
  readonly VITE_API_BASE?: string;
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
