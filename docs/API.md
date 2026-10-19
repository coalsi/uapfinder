# SKY-NET server HTTP API (v1)

This is the contract between the server (`server/`), the edge nodes
(`edge/`) and the dashboard (`dashboard/`). All bodies are JSON unless noted.
Timestamps are integer nanoseconds since the Unix epoch. Positions are WGS84
degrees and metres above the ellipsoid (`alt_m`), the same height reference
CesiumJS uses for `Cartesian3.fromDegrees`.

Base path: `/api/v1`. CORS is enabled for the origins in
`SKYNET_CORS_ORIGINS` so the dashboard can be hosted separately.

## Authentication

| Caller | How |
|--------|-----|
| Admin (node provisioning) | `Authorization: Bearer <SKYNET_ADMIN_TOKEN>`. Refused with `403` for requests that arrive through Tailscale Funnel (tailscaled marks them with `Tailscale-Funnel-Request`), so on a Funnel deployment admin calls must come from the tailnet or the host. |
| Edge node (HTTP) | `X-Skynet-Node`, `X-Skynet-Timestamp`, `X-Skynet-Signature` headers, see [PROTOCOL.md](PROTOCOL.md#signing) |
| Edge node (ingest) | each NDJSON line is individually signed; no request auth |
| Dashboard / public | none (read-only). Node positions in public responses are rounded to `SKYNET_PUBLIC_LOCATION_DECIMALS` decimal places (default 2, about 1 km) to protect volunteer households. |

Errors use FastAPI's shape: `{"detail": "…"}` with an appropriate status.

## Shared objects

**Node**
```json
{
  "node_id": "node-a",
  "label": "Surprise AZ rooftop",
  "lat": 33.63, "lon": -112.37, "alt_m": 357.2,
  "yaw_deg": 0.0, "pitch_deg": 45.0, "roll_deg": 0.0,
  "fov_h_deg": 90.0, "fov_v_deg": 58.7,
  "last_seen_ns": 1782345600000000000,
  "online": true,
  "status": {"fps": 59.8, "mem_used_mb": 3120},
  "software_version": "0.1.0"
}
```
`label` may be null. Camera fields are null until the first heartbeat.

**TrackPoint**
```json
{"t_ns": 1782345600000000000, "lat": 33.55, "lon": -112.2, "alt_m": 9144.0,
 "sigma_m": 41.2, "error_margin_m": 12.5, "node_ids": ["node-a", "node-b"]}
```
`sigma_m` is the 1-sigma 3D position uncertainty from the error model in
[MATH.md](MATH.md); `error_margin_m` is the closest-approach distance between
the rays (the blueprint's error metric).

**Track** (a 3D track built from successive triangulations of the same object)
```json
{
  "track_id": "T-3f2a9c",
  "first_ns": 1782345600000000000,
  "last_ns": 1782345612000000000,
  "node_ids": ["node-a", "node-b", "node-c"],
  "point_count": 118,
  "last_point": { "…TrackPoint…" },
  "speed_mps": 2210.0,
  "max_speed_mps": 2380.5,
  "max_accel_mps2": 412.0,
  "classification": "hypersonic",
  "flags": ["hypersonic", "high_g"],
  "points": [ "…TrackPoint…" ]
}
```
`points` is only present on `GET /tracks/{track_id}` and, truncated to the
most recent 200, on `GET /tracks`.

`classification` is one of `unknown` (default), `ballistic`, `hypersonic`
(speed > 1715 m/s, Mach 5 at sea level), `high_g` (acceleration > 98 m/s²,
10 g) or `stationary` (speed < 2 m/s for 30 s). `flags` lists every rule
that fired.

**Event** (the live feed)
```json
{"id": "1782345600012-0", "type": "triangulation", "t_ns": 1782345600000000000, "data": { … }}
```

| `type` | `data` |
|--------|--------|
| `triangulation` | `{"event_id", "track_id", "t_ns", "lat", "lon", "alt_m", "sigma_m", "error_margin_m", "parallax_deg", "rays": [{"node_id", "az_deg", "el_deg", "node_lat", "node_lon", "node_alt_m"}]}`. Ray node positions are rounded like public Node positions, and `az_deg`/`el_deg` are recomputed from that rounded position to the published point. The measured lines of sight are never published: back-projecting a few of them through their 3D points would locate a volunteer's camera to within metres. |
| `track` | a Track without `points` |
| `node` | a Node |
| `clip` | `{"request_id", "event_id", "track_id", "node_id", "status": "requested" \| "uploaded" \| "failed", "download_url": null \| "…"}` |

`id` is an opaque, monotonically increasing cursor string.

## Endpoints

### `GET /api/v1/health`
`{"status": "ok", "version": "0.1.0", "mode": "standalone" | "vercel", "state_backend": "memory" | "redis", "state": "ok" | "error", "db": "ok" | "disabled" | "error", "udp_port": 9999 | null, "time_ns": …}`

### `GET /api/v1/stats`
```json
{
  "uptime_s": 3600.0,
  "packets_total": 123456, "packets_per_s": 812.4,
  "rejected_total": {"bad_signature": 3, "stale": 10},
  "matches_total": 4567, "matches_per_s": 31.0,
  "active_nodes": 12, "active_tracks": 2,
  "processing_latency_ms": {"p50": 0.08, "p95": 0.2, "p99": 0.4, "max": 3.1},
  "backsignal_latency_ms": {"p50": 0.3, "p95": 0.6, "p99": 1.1, "max": 4.0}
}
```
Latencies are measured inside the server from message receipt to completion
of processing, and to the hand-off of the `clip_request` datagram (UDP) or
its enqueueing (HTTPS); the percentiles cover the last 30 s. The rates are
averaged over the last 10 s. A `counters` object with the raw counters
(searches, matches, tracks created, clip requests by transport, …) is
included for operators.

### `POST /api/v1/nodes` (admin)
Request: `{"label": "optional", "node_id": "optional custom id"}`
Response `201`:
```json
{"node_id": "4f9a…", "node_key": "<64 hex>", "ingest_url": "https://…/api/v1/ingest", "udp_endpoint": "host:9999" }
```
`udp_endpoint` is null when the deployment has no UDP listener (Vercel).

### `GET /api/v1/nodes`
`{"nodes": [Node, …]}`; query `online_only=true` filters to active nodes.

### `POST /api/v1/ingest`
Body: `Content-Type: application/x-ndjson`, at most 2000 lines, each one
signed `telemetry` or `heartbeat` message.

Response `200`:
```json
{"accepted": 37, "rejected": [{"line": 4, "reason": "stale"}], "commands": ["{\"type\":\"clip_request\",…,\"sig\":\"…\"}"]}
```
`rejected[].line` is 1-based and counts blank lines. `commands` holds
pending server-to-node messages, as raw signed JSON strings so the node can
verify the signature over the exact bytes. Only commands for node_ids that
appear in validly signed lines of this request are returned, and each
command is returned once. A node that reports over HTTPS gets its clip
requests here even if it sent UDP shortly before: the transport of a node's
most recent message decides how its clip requests are delivered.

### `GET /api/v1/nodes/{node_id}/commands` (node auth)
`{"commands": ["…signed clip_request…"]}`; same once-only semantics.

### `GET /api/v1/tracks`
Query: `since_ns` (default: last hour), `limit` (default 100, max 1000),
`classification` (optional filter). Response `{"tracks": [Track, …]}`, newest
first.

### `GET /api/v1/tracks/{track_id}`
A Track with all points plus `"clips": [{"request_id", "node_id", "status", "download_url"}]`.

### `GET /api/v1/events`
Query: `after` (cursor; omit for the most recent `limit` events), `limit`
(default 200, max 1000). Response: `{"events": [Event, …], "cursor": "…"}`,
oldest first. Poll with the returned cursor. Works on every deployment and
is the dashboard's fallback when WebSockets are unavailable.

### `WS /api/v1/live`
Server pushes each Event as a JSON text frame as it happens. Clients should
reconnect with backoff and backfill from `GET /events?after=<last id>`
(Vercel closes WebSocket connections when the function's maximum duration
is reached).

### `GET /api/v1/coverage`
Stereo coverage map for a bounding box.

Query: `min_lat`, `min_lon`, `max_lat`, `max_lon` (required), `cell_km`
(default 10), `ref_alt_m` (default 3000, the altitude slice evaluated),
`min_elevation_deg` (default 10), `min_parallax_deg` (default 5). The grid
is capped at 10 000 cells; larger requests get `400`.

A node *sees* a cell when the point at `ref_alt_m` above the cell centre is
within 80 miles horizontally, above `min_elevation_deg`, and inside the
camera's field of view. A cell is `stereo` when at least one pair of nodes
seeing it has a parallax angle of at least `min_parallax_deg` there, `mono`
when exactly one node (or only low-parallax pairs) sees it, and `blind`
otherwise.

Response (GeoJSON):
```json
{
  "type": "FeatureCollection",
  "params": {"cell_km": 10, "ref_alt_m": 3000, "…": "…"},
  "summary": {"blind": 812, "mono": 140, "stereo": 48},
  "suggestions": [{"lat": 33.9, "lon": -112.8, "gain_cells": 37}],
  "features": [
    {"type": "Feature",
     "geometry": {"type": "Polygon", "coordinates": [[[lon, lat], …]]},
     "properties": {"seeing": 2, "stereo_pairs": 1, "status": "stereo"}}
  ]
}
```
`suggestions` are up to 10 cell centres where one new zenith-pointing node
(90° FOV) would turn the most `blind`/`mono` cells into `stereo`, used to
steer rural deployment.

### Clip upload (node auth)

1. `POST /api/v1/clips/{request_id}/upload-target` → `{"method": "PUT", "url": "…", "headers": {"Content-Type": "video/mp4"}}`
2. Upload the file with that method, URL and headers. Two kinds of target:
   * **Server storage** (self-hosted): `PUT {SKYNET_PUBLIC_URL}/api/v1/clips/{request_id}/data`.
     The node adds its own node-auth headers (they are not in the returned
     `headers`), signed over `PUT`, the path, the timestamp and the body's
     SHA-256 (±60 s window, replays refused). The server streams the body to
     disk and checks the signature against its digest before keeping it.
     Responses: `200 {"bytes", "sha256"}`, `401` bad or missing auth, `403`
     the clip belongs to another node, `413` too large, `404` when the
     deployment stores clips in S3. The URL never points at a loopback or
     `0.0.0.0` host; set `SKYNET_PUBLIC_URL` to the address nodes can reach.
   * **Presigned object storage** (S3, R2, Backblaze; required on Vercel):
     upload exactly as returned, with no node headers.
3. `POST /api/v1/clips/{request_id}/complete` with
   `{"bytes": 1234567, "sha256": "…", "content_type": "video/mp4", "t_start_ns": …, "t_end_ns": …, "frames": 600}` → `{"status": "uploaded"}`

`GET /api/v1/clips/{request_id}` (public) → `{"request_id", "event_id", "track_id", "node_id", "status", "download_url", "t_start_ns", "t_end_ns", "bytes"}`.

### Volunteer sign-up

The public website's "Host a node" interest form. Requires the database
(responds `503` with `{"detail": "sign-up unavailable"}` when the server
runs without one). Stores only what volunteers type in; exact street
addresses are never requested.

`POST /api/v1/volunteers` (public; rate-limited to 5 requests per hour per
client IP; body at most 4 KB)
```json
{
  "email": "jane@example.org",
  "name": "Jane",
  "country": "US",
  "region": "Arizona",
  "city": "Surprise",
  "postal_code": "85374",
  "approx_lat": 33.63,
  "approx_lon": -112.37,
  "site_type": "rooftop",
  "has_hardware": "none",
  "interests": ["host_node", "software"],
  "notes": "Clear view to the south-west.",
  "consent": true,
  "website": ""
}
```
Required: `email` (valid address, ≤254 chars), `country` (ISO 3166-1
alpha-2), `consent` (must be `true`). Optional: `name` (≤100), `region`,
`city` (≤100 each), `postal_code` (≤20), `approx_lat`/`approx_lon` (the
server rounds them to 2 decimals, about 1 km, before storing),
`site_type` (`rooftop` | `yard` | `rural_land` | `other`), `has_hardware`
(`none` | `some` | `full`), `interests` (subset of `host_node`, `software`,
`data_analysis`, `outreach`), `notes` (≤1000). `website` is a honeypot:
humans leave it empty; non-empty submissions are silently discarded but
still get `201`.

Response `201`: `{"status": "received"}`. A repeat submission with the same
email updates the earlier record and gets the same response, so the
endpoint does not reveal who has signed up. Validation errors return `422`.

`GET /api/v1/volunteers/summary` (public)
```json
{"total": 42, "cells": [{"lat": 33.5, "lon": -112.5, "count": 7}]}
```
Counts per 1° × 1° cell (cell centre given). Cells with fewer than 3
volunteers are omitted so individuals cannot be located.

`GET /api/v1/volunteers` (admin) → `{"volunteers": [{…all stored fields…, "id", "created_at", "updated_at"}]}`;
`?format=csv` returns `text/csv`.

`DELETE /api/v1/volunteers/{id}` (admin) → `204`, for removal requests.

### `GET /metrics` (alias `GET /api/v1/metrics`)
Prometheus text exposition of the counters behind `/stats`. The alias is
reachable on Vercel, where only `/api/*` is routed to the function.
