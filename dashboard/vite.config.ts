import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { viteStaticCopy } from "vite-plugin-static-copy";

// CesiumJS loads its web workers, widget CSS/images and bundled assets
// (including the offline Natural Earth II imagery) at runtime from
// CESIUM_BASE_URL, so they are copied verbatim next to the bundle. This follows
// https://github.com/CesiumGS/cesium-vite-example. vite-plugin-static-copy v4
// always preserves the source directory structure, so the four leading
// segments (node_modules/cesium/Build/Cesium) are stripped.
const cesiumSource = "node_modules/cesium/Build/Cesium";
const cesiumBaseUrl = "cesiumStatic";

export default defineConfig({
  // Relative asset URLs: the build works from any path on any static host.
  base: "./",
  define: {
    CESIUM_BASE_URL: JSON.stringify(`./${cesiumBaseUrl}`),
  },
  plugins: [
    react(),
    viteStaticCopy({
      targets: ["ThirdParty", "Workers", "Assets", "Widgets"].map((dir) => ({
        src: `${cesiumSource}/${dir}`,
        dest: cesiumBaseUrl,
        rename: { stripBase: 4 },
      })),
    }),
  ],
  server: { port: 5173, strictPort: true },
  preview: { port: 5174, strictPort: true },
  build: {
    // The CesiumJS chunk is ~4 MB minified by itself; it is split out so it is
    // cached independently of app releases.
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
});
