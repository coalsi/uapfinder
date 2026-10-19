// Must stay the first import: sets CESIUM_BASE_URL before any CesiumJS code runs.
import "./cesiumBase";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ApiClient } from "./api/client";
import { readConfig } from "./config";
import { createApiBackend } from "./data/apiBackend";
import { createDemoBackend } from "./demo/simulator";
import { createStore } from "./store/store";
import "./styles.css";

const config = readConfig(import.meta.env, window.location.href);
const backend = config.demo ? createDemoBackend() : createApiBackend(new ApiClient(config.apiBase));
const store = createStore();

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <App config={config} backend={backend} store={store} />
  </StrictMode>,
);
