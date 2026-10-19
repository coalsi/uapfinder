# UAP Finder live map (SKY-NET dashboard)

The public 3D map of the UAP Finder network: volunteer camera nodes and their
fields of view, live stereo-triangulated tracks, and the coverage map that
shows where the network is blind and where a new node would help most.

React 19 + TypeScript (strict) + Vite 8 + CesiumJS 1.146. It is a static
site: it reads the server's public, read-only API ([docs/API.md](../docs/API.md))
and needs no paid map token.

![Desktop: coverage view](screenshots/desktop-coverage.png)

## Quick start

```sh
npm ci
npm run dev            # http://localhost:5173/?demo=1  (simulated data)
```

Without `?demo=1` the dev server talks to the API on the same origin; point it
at a running server with `VITE_API_BASE=http://localhost:8000 npm run dev`.

| Script | What it does |
|--------|--------------|
| `npm run dev` | Vite dev server on port 5173 |
| `npm run build` | `tsc` typecheck (app + config) and production build into `dist/` (base `/`, or `VITE_BASE_PATH`) |
| `npm run build:map` | Same, with base `/map/` (the combined deploy) |
| `npm run preview` | Serve `dist/` on port 5174 |
| `npm test` | Vitest unit tests |
| `npm run screenshot` | Builds for `/` and `/map/`, serves both in demo mode, saves screenshots (see below) |
| `npm run fixtures` | Regenerate the camera/geodesy fixtures from `common/skynet_common` |

## Configuration

Build-time variables (Vite inlines them, so set them when running
`npm run build`, or put them in `.env.production.local`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `VITE_API_BASE` | *(empty: same origin)* | Root URL of the SKY-NET server, e.g. `https://studio.tail1234.ts.net`. The client adds `/api/v1/…`; a trailing `/api/v1` is tolerated. The live feed uses `wss://` for an `https://` base and `ws://` for `http://`. |
| `VITE_BASE_PATH` | `/` | Public path the dashboard is served from, e.g. `/map/`. Sets Vite's `base`, so the bundle and CesiumJS's static files (`<base>cesiumStatic/`) resolve under it. `npm run build:map` is the same as `VITE_BASE_PATH=/map/`. |
| `VITE_SITE_URL` | `/` | The public website: target of "About UAP Finder"; "Host a node" goes to its `host/` page. |
| `VITE_CESIUM_ION_TOKEN` | *(none)* | Optional. Enables Cesium World Terrain, Cesium World Imagery and the place-search box. Without it nothing from Cesium ion is used. |
| `VITE_DEMO` | *(off)* | `1` starts in demo mode. |
| `VITE_IMAGERY_URL` / `VITE_IMAGERY_CREDIT` | OpenStreetMap | Optional XYZ tile template (`{z}/{x}/{y}`) and its HTML attribution, to use another tile provider. |

Per page load: `?demo=1` / `?demo=0` overrides `VITE_DEMO`;
`?imagery=offline` uses only the Natural Earth II imagery bundled with
CesiumJS (no tile server), `?imagery=osm` forces OpenStreetMap;
`?layer=coverage|tracks|nodes` opens that panel (also the drawer on phones),
as the website's "Where nodes are needed" link (`/map/?layer=coverage`) does.

### Map data

Without a token the globe is the WGS84 ellipsoid with dimmed OpenStreetMap
tiles over the bundled Natural Earth II imagery; the OpenStreetMap credit is
shown on the globe while its tiles are visible. All heights are metres above
the WGS84 ellipsoid (the API's `alt_m`), which is also what Cesium World
Terrain uses. `tile.openstreetmap.org` is meant for light use
([tile usage policy](https://operations.osmfoundation.org/policies/tiles/)); for a busy
public deployment set `VITE_IMAGERY_URL` to a provider you have an agreement
with, or set an ion token.

## Demo mode

`?demo=1` (or `VITE_DEMO=1`) runs a client-side simulator around Phoenix, AZ:
six nodes (one offline), five kinds of tracks (hypersonic, high-g, ballistic,
stationary balloon, slow unknown), clip requests and uploads, stats and a
coverage model that follows the rules in docs/API.md. It produces the same
API-shaped data and goes through the same store as the live server. The page
is marked **DEMO DATA** in the header and on the globe.

## What it shows

- **Header:** packets/s, matches/s, active nodes and tracks, back-signal
  latency p50/p99 (`GET /stats`, every 2 s) and the connection state.
- **Nodes:** green online / grey offline markers with labels; click for
  status, frame rate, memory, software version and last seen. Public node
  positions are rounded by the server (about 1 km).
- **Camera views:** each node's viewing frustum from its reported attitude and
  FOV, out to a selectable range. The camera maths is a port of
  `common/skynet_common/camera.py` (yaw = azimuth clockwise from true north,
  pitch = elevation, roll = right side down; OpenCV camera frame), placed on
  the globe with `Transforms.eastNorthUpToFixedFrame`, and unit-tested against
  Python-computed fixtures.
- **Tracks:** 3D polylines coloured by classification with the latest point
  highlighted; each `triangulation` event briefly flashes the rays from the
  contributing nodes. The track list (newest first) shows speed, max speed,
  max acceleration in g, class and node count; selecting a track flies to it
  and loads `GET /tracks/{id}` (all points and clip download links).
- **Coverage:** `GET /coverage` for the area in view (debounced, cell size
  chosen from the view size, at most ~2 500 cells, refreshed every minute),
  red blind / amber mono / green stereo cells at the selected altitude slice,
  summary counts, and the server's "deploy a node here" suggestions with the
  number of cells each would turn stereo.

## Data flow

`src/data/apiBackend.ts` fetches the most recent events (for the cursor), the
nodes and recent tracks, then starts `src/api/live.ts`:

1. `WS /api/v1/live` with exponential-backoff reconnect (1 s doubling to 30 s, ±20 % jitter);
2. on every (re)open, a backfill of `GET /events?after=<cursor>` until caught up;
3. while the socket is down (or never opens, e.g. on Vercel), polling
   `GET /events?after=<cursor>` every second, backing off on errors.

The cursor only ever marks a point up to which every event has been
delivered, so reconnects never skip events; duplicates are dropped by id and
the reducer is idempotent. `GET /nodes` is re-read every 30 s so nodes that go
quiet show as offline. All of it lands in one reducer (`src/store/reducer.ts`)
with bounded memory: 100 tracks, 1 000 points per track, 50 clips per track,
32 pending ray flashes.

## Deployment

The output in `dist/` is static: `index.html`, hashed bundles in `assets/`
and CesiumJS's Workers/Assets/Widgets/ThirdParty in `cesiumStatic/`.

**Combined Vercel deploy (public site at `/`, map at `/map/`):** build with
the `/map/` base and publish `dist/` as the `map/` directory of the site
output, e.g.

```sh
cd dashboard && npm ci && npm run build:map
cp -r dist ../<site output>/map
```

`/map` and `/map/` both work (asset URLs are absolute under `/map/`). If the
API runs elsewhere, set `VITE_API_BASE` for that build. Files under
`/map/assets/` are content-hashed and can be cached forever; `cesiumStatic/`
changes only with the CesiumJS version.

**Any static host:** `npm run build` (or with `VITE_BASE_PATH` for a sub-path)
and upload `dist/`. No server-side routing is needed.

**API on another origin** (e.g. a Mac Studio behind Tailscale Funnel): set
`VITE_API_BASE=https://<machine>.<tailnet>.ts.net`, and add the dashboard's
origin to the server's `SKYNET_CORS_ORIGINS`. Requests are plain GETs with
only CORS-safelisted headers (no preflight, no cookies); the WebSocket uses
`wss://`. An `https://` page cannot use an `http://` API (mixed content), so
expose the API over HTTPS.

## Tests and screenshots

- `npm test`: camera/frustum maths against `src/geo/__fixtures__/camera.json`
  (generated from `common/skynet_common` by `npm run fixtures`), the reducer,
  the API client (URLs, wss derivation, errors, safe links), backoff, the live
  feed (cursor, backfill, polling fallback, reconnect backoff; fake socket and
  timers), coverage requests, config, and a simulator smoke test.
- `npm run screenshot`: builds for `/` and `/map/`, serves the `/map/` build
  under a stand-in site the way the combined deploy does, renders demo mode in
  headless Chromium (software WebGL) and writes `screenshots/desktop-coverage.png`,
  `mobile-overview.png` (at `/`), `desktop-track.png` and `mobile-tracks.png`
  (at `/map/`). It fails on page errors, failed requests, CesiumJS files not
  loading from under the base path, or a missing OpenStreetMap credit (checked
  with tile requests answered locally). It uses Playwright's Chromium, or the
  newest one in `PLAYWRIGHT_BROWSERS_PATH`, or `CHROMIUM_PATH`.

## Source layout

```
src/api/        wire types, REST client, backoff, live feed (WebSocket + polling)
src/data/       Backend interface, API backend, React hooks (feed, stats, coverage)
src/demo/       client-side simulator and coverage model for demo mode
src/geo/        TS port of skynet_common camera/geodesy, coverage request sizing
src/globe/      CesiumJS viewer and layers (nodes, frustums, tracks, ray flashes, coverage)
src/store/      reducer and store
src/components/ header, layer bar, legend, side panel / mobile drawer
```
