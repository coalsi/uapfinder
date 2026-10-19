import { cpSync, createReadStream, existsSync, statSync } from "node:fs";
import { extname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { type Plugin, type ResolvedConfig, defineConfig, loadEnv } from "vite";
import { CESIUM_STATIC_DIR, normalizeBase } from "./src/basePath.ts";

const root = fileURLToPath(new URL(".", import.meta.url));

// CesiumJS loads its web workers, widget CSS/images and bundled assets
// (including the offline Natural Earth II imagery) at runtime from
// CESIUM_BASE_URL, so those directories are served verbatim next to the
// bundle, as in https://github.com/CesiumGS/cesium-vite-example. The page sets
// CESIUM_BASE_URL to `${BASE_URL}cesiumStatic/` (src/cesiumBase.ts), so the
// app works under any base path, e.g. /map/ in the combined Vercel deploy.
const CESIUM_BUILD = join(root, "node_modules", "cesium", "Build", "Cesium");
const CESIUM_DIRS = ["Workers", "ThirdParty", "Assets", "Widgets"] as const;

const MIME: Record<string, string> = {
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".wasm": "application/wasm",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".gif": "image/gif",
  ".svg": "image/svg+xml",
  ".xml": "application/xml; charset=utf-8",
};

/** Copies CesiumJS's static directories into the build, and serves them from node_modules in dev. */
function cesiumStatic(): Plugin {
  let config: ResolvedConfig;
  return {
    name: "uapfinder:cesium-static",
    configResolved(resolved) {
      config = resolved;
    },
    configureServer(server) {
      const prefix = `${new URL(config.base, "http://localhost").pathname}${CESIUM_STATIC_DIR}/`;
      server.middlewares.use((req, res, next) => {
        const pathname = (req.url ?? "").split(/[?#]/)[0] ?? "";
        if (!pathname.startsWith(prefix)) return next();
        let rel: string;
        try {
          rel = decodeURIComponent(pathname.slice(prefix.length));
        } catch {
          return next();
        }
        const file = resolve(CESIUM_BUILD, rel);
        const top = relative(CESIUM_BUILD, file).split(sep)[0];
        if (!CESIUM_DIRS.some((d) => d === top) || !existsSync(file) || !statSync(file).isFile()) return next();
        res.setHeader("Content-Type", MIME[extname(file).toLowerCase()] ?? "application/octet-stream");
        createReadStream(file).pipe(res);
      });
    },
    writeBundle() {
      const outDir = resolve(config.root, config.build.outDir);
      for (const dir of CESIUM_DIRS) {
        cpSync(join(CESIUM_BUILD, dir), join(outDir, CESIUM_STATIC_DIR, dir), { recursive: true });
      }
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, root, "VITE_");
  return {
    root,
    // "/" by default; "/map/" when the dashboard is mounted under the public
    // site (VITE_BASE_PATH=/map/ or `npm run build:map`).
    base: normalizeBase(env.VITE_BASE_PATH),
    plugins: [react(), cesiumStatic()],
    server: { port: 5173, strictPort: true },
    preview: { port: 5174, strictPort: true },
    build: {
      // The CesiumJS chunk is ~4 MB minified by itself; it is split out so it
      // is cached independently of app releases.
      chunkSizeWarningLimit: 6000,
      rolldownOptions: {
        output: {
          codeSplitting: {
            groups: [
              { name: "cesium", test: /node_modules[\\/](cesium|@cesium)[\\/]/ },
              { name: "react", test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/ },
            ],
          },
        },
      },
    },
  };
});
