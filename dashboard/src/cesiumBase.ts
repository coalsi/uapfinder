import { CESIUM_STATIC_DIR } from "./basePath";

// CesiumJS resolves its Workers, Assets, Widgets and ThirdParty files against
// the global CESIUM_BASE_URL the first time it needs one. vite.config.ts copies
// them to `<base>/cesiumStatic/`; BASE_URL is Vite's resolved `base`, so this
// also holds when the dashboard is mounted under /map/. Imported first by
// main.tsx, before any CesiumJS code can run.
window.CESIUM_BASE_URL = `${import.meta.env.BASE_URL}${CESIUM_STATIC_DIR}/`;
