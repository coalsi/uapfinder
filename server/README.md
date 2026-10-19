# SKY-NET server

Ingestion, stereo matchmaking, 3D tracking, persistence and the HTTP API of
SKY-NET (UAP Finder). One codebase runs two ways:

| | standalone (primary) | vercel (alternative) |
|---|---|---|
| Entry | `skynet-server serve` (uvicorn + UDP on one event loop) | `api/index.py` (Vercel Python Function) |
| Node ingest | UDP `:9999` and `POST /api/v1/ingest` | `POST /api/v1/ingest` only |
| Back-signal | UDP datagram to the node's last source address (or command queue) | command queue (ingest response / `GET .../commands`) |
| State | `memory` (fastest, one process) or `redis` | `redis` (Upstash) |
| Database | optional PostGIS, batched background writes | optional PostGIS (Neon), written before responding |
| Clips | local disk or S3-compatible | S3-compatible (R2, S3, B2) |
| Live feed | `WS /api/v1/live` (in-process; Redis stream with the redis backend) | `WS /api/v1/live` from the Redis stream, closed before the function's max duration |

The wire protocol and API are specified in [docs/PROTOCOL.md](../docs/PROTOCOL.md)
and [docs/API.md](../docs/API.md); the geometry and error model in
[docs/MATH.md](../docs/MATH.md).

## Code map

| Module | Role |
|---|---|
| `engine.py` | the ingest pipeline shared by UDP and HTTPS: decode, freshness, replay, neighbours, matching, tracks, clip requests, commit, back-signal |
| `decode.py` | `decode_message` restructured for throughput (cached node keys, orjson); parity-tested against the reference |
| `matchmaker.py` | blueprint section 3: candidates within 50 ms or interpolated between bracketing samples, gating, N-ray least squares, search plans (guided by learnt partners, full searches pruned by camera view cones) |
| `fastmath.py` | pure-`math` versions of `triangulate_many`, `position_covariance` and `gate(triangulate_rays)`, tested against `skynet_common` |
| `tracks.py` | 3D track linking, polynomial-fit kinematics, `hypersonic` / `high_g` / `ballistic` / `stationary` classification |
| `state/memory.py`, `state/redis.py` | the state backends (one contract, one test-suite) |
| `udp.py` | UDP listener: drains the socket in batches, sends clip requests from the same socket |
| `app.py` | FastAPI app and process runtime (lifespan, CORS, node and admin auth) |
| `coverage.py` | vectorised stereo coverage maps and node-siting suggestions |
| `storage.py` | local clip storage and SigV4 presigned URLs (no boto3) |
| `persistence.py`, `migrations/` | asyncpg + PostGIS, idempotent migrations |
| `volunteers.py` | "Host a node" sign-up validation and CSV export |
| `sim.py` | synthetic network used by tests |
| `bench/bench_ingest.py` | UDP ingest benchmark |
| `bench/bench_inprocess.py` | single-process matching capacity, no network |

## Run standalone (Linux or macOS)

Python 3.10+ (tested with 3.13). From the repository root:

```bash
python3 -m venv server/.venv
server/.venv/bin/pip install -e common -e 'server[test]'

cp server/.env.example server/.env
server/.venv/bin/skynet-server keygen          # -> SKYNET_MASTER_KEY in server/.env
# set SKYNET_ADMIN_TOKEN and SKYNET_PUBLIC_URL (e.g. http://mac-studio:8000) in server/.env

server/.venv/bin/skynet-server --env-file server/.env serve
# HTTP API on :8000, UDP ingest on :9999 (uvloop is used when installed)
```

Provision a node (the response carries its key and the endpoints to use):

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/nodes \
  -H "Authorization: Bearer $SKYNET_ADMIN_TOKEN" -d '{"label": "Surprise AZ rooftop"}'
```

`skynet-server node-key <node_id>` prints the same key offline.

Optional pieces:

* **PostGIS** (history beyond the live state, volunteers): set
  `SKYNET_DATABASE_URL`, then `skynet-server --env-file server/.env migrate`
  (idempotent; it records applied files in `schema_migrations`). On macOS,
  Postgres.app and Homebrew's `postgresql` + `postgis` both work.
* **Redis** (`SKYNET_STATE_BACKEND=redis`): state survives restarts and several
  instances can share it; it costs about twice the CPU per message of the
  memory backend.
* **UDP buffers**: bursts are absorbed by the socket receive buffer
  (`SKYNET_UDP_RCVBUF`, 8 MB requested). Linux caps it at `net.core.rmem_max`
  (`sudo sysctl -w net.core.rmem_max=16777216`); macOS at
  `kern.ipc.maxsockbuf` (the server steps down until the size is accepted).

The coordinator's Mac Studio + Tailscale guide covers running this as a
service and exposing it. Server-side facts that guide relies on:

* nodes on the tailnet send UDP to `SKYNET_UDP_PUBLIC_ENDPOINT`
  (e.g. `mac-studio:9999`) and use `SKYNET_PUBLIC_URL` for HTTP;
  clip-request `upload_url`s and upload targets are built from
  `SKYNET_PUBLIC_URL`. When it is unset the server logs a warning and uses
  the request's base URL for HTTPS-ingest nodes or `http://<hostname>:<port>`,
  never a loopback or `0.0.0.0` address;
* behind Tailscale Funnel (a reverse proxy on 127.0.0.1) uvicorn already takes
  the client address from `X-Forwarded-For`; set `SKYNET_CORS_ORIGINS` to the
  website/dashboard origin so browsers may call the API cross-origin.

## Configuration

Everything is an environment variable; [`.env.example`](.env.example) lists
all of them with defaults. The essentials:

| Variable | Meaning |
|---|---|
| `SKYNET_MASTER_KEY` | required; node keys are `HMAC-SHA256(master, "skynet-node-key:" + node_id)` |
| `SKYNET_ADMIN_TOKEN` | required for admin endpoints |
| `SKYNET_PUBLIC_URL` | base URL nodes use for clip uploads |
| `SKYNET_STATE_BACKEND` | `memory` / `redis` (`SKYNET_REDIS_URL`, else `REDIS_URL`, `KV_URL`; `rediss://` for TLS) |
| `SKYNET_DATABASE_URL` | PostGIS (else `DATABASE_URL`, `POSTGRES_URL`) |
| `SKYNET_STORAGE` | `local` / `s3` / `none` (`SKYNET_S3_*`) |
| `SKYNET_CORS_ORIGINS` | comma-separated allowed browser origins |
| `SKYNET_TRUST_PROXY` | client IPs from the last `X-Forwarded-For` entry (default on in vercel mode) |
| `SKYNET_ALLOW_UNSIGNED` | **insecure** development switch accepting unsigned messages (off) |

## Behaviour worth knowing

* **Replays** are dropped per (node, `seq`, `timestamp_ns`): exact duplicates
  and replays always match on both, while a node that restarts and resets
  `seq` is not locked out. Messages outside +-10 s are `stale` first.
* **Ingest** (`POST /api/v1/ingest`): `rejected[].line` is 1-based and counts
  blank lines; commands are returned once, only for nodes with a validly
  signed (possibly stale) line in the request.
* **Node-authenticated HTTP** (`GET .../commands`, `upload-target`,
  `complete`, and the server's own `PUT /api/v1/clips/{id}/data`) checks the
  X-Skynet headers of PROTOCOL.md, a +-60 s timestamp window and refuses a
  replayed signature. The `PUT` is signed over `PUT`, its path, the timestamp
  and the SHA-256 of the uploaded body: the server streams the body to a
  temporary file, verifies the signature against its digest, and only then
  keeps it. Presigned object-storage URLs take no node headers.
* **Matching**: the packet is stored before neighbours are queried, so of two
  simultaneous packets at least one sees the other, even across instances.
  A neighbour's line of sight is interpolated between two samples of its
  local track that bracket the packet time (at most 250 ms apart), otherwise
  the nearest sample within 50 ms is used. Solutions use the packet's ray and
  up to `SKYNET_MATCH_MAX_RAYS - 1` neighbours (default 8 rays in total);
  inconsistent rays are dropped. Each detection pair produces at most one
  triangulation.
* **Search cost control**: once two local tracks have matched, later packets
  only test those partners (guided search). Full searches skip neighbours
  whose camera view cone (attitude + FOV from heartbeats, 3 degree margin)
  does not contain any part of the packet's line of sight.
* **Same-frame refinements**: a frame seen by k nodes yields up to k-1
  solutions as its packets arrive. A new one becomes a track point (and a
  `triangulation` event, and a database row) only if it improves the frame's
  best sigma by 20%; links, joining nodes and clip requests are unaffected.
* **Kinematics**: speed from a 1 s line fit, acceleration from a 3 s
  quadratic (a 1 s fit cannot resolve 10 g at tens of metres of point noise),
  ballistic/stationary from a 30 s decimated history; flags use 3-3.5 sigma
  bounds because a track is refitted twice a second.
* **Volunteer privacy**: public outputs (Node objects, triangulation rays in
  `/events` and `WS /live`) carry node positions rounded to
  `SKYNET_PUBLIC_LOCATION_DECIMALS` (default 2, ~1 km) and heights rounded to
  10 m; published ray az/el are recomputed from that rounded position to the
  published point (the measured lines of sight, which back-projected would
  locate a camera to metres, are kept only in the database); coverage maps are
  computed from the rounded positions too. Track points carry node ids only.
* **Overload**: the UDP queue is bounded (2048 datagrams) and sheds the
  oldest (`udp_dropped_overload` in `/stats`), so latency stays bounded.
* `/stats` latency percentiles cover the last 30 s (Redis: the last 4096
  sampled values across instances).

## Deploy on Vercel

The same repository also hosts the website (`/`) and dashboard (`/map/`);
`vercel.json` routes `/api/*` to `api/index.py`.

1. Import the GitHub repository as a Vercel project (the coordinator's
   build settings serve `site/` and `dashboard/`).
2. Marketplace: add **Upstash for Redis** (injects `REDIS_URL` / `KV_URL`)
   and **Neon** Postgres (injects `DATABASE_URL`, `DATABASE_URL_UNPOOLED`).
   Neon supports PostGIS; the first migration runs `CREATE EXTENSION postgis`.
3. Environment variables: `SKYNET_MASTER_KEY`, `SKYNET_ADMIN_TOKEN`, and for
   clips `SKYNET_STORAGE=s3` with `SKYNET_S3_BUCKET`, `SKYNET_S3_ENDPOINT`
   (e.g. Cloudflare R2), `SKYNET_S3_REGION` (`auto` for R2),
   `SKYNET_S3_ACCESS_KEY_ID`, `SKYNET_S3_SECRET_ACCESS_KEY`. Without
   `SKYNET_PUBLIC_URL` the production domain (`VERCEL_PROJECT_PRODUCTION_URL`)
   is used. Configure the bucket's CORS only if browsers upload (nodes do not).
4. Run the migrations once against the direct Neon URL from a machine with
   the repository:
   `SKYNET_DATABASE_URL_UNPOOLED=... server/.venv/bin/skynet-server migrate`.
5. Check `GET /api/v1/health`: `mode=vercel`, `state=ok`, `db=ok`. A
   configuration error is reported there (HTTP 500 with the reason) instead of
   crashing the function.
6. Nodes use `https://<project>/api/v1/ingest` (no UDP on Vercel) and get
   their clip requests in ingest responses or from `GET .../commands`.

How the function is built (researched in the Vercel builder source,
`vercel/vercel` `packages/python`): `requirements.txt` at the root is
converted into a uv project and installed; the function bundle contains the
project files minus `excludeFiles`, so `api/index.py` imports `server/` and
`common/` from source via `sys.path` instead of relying on local path
requirements. `.python-version` pins 3.13 (the runtime supports 3.12-3.14;
3.12 is its default). `maxDuration` is 300 s (the Hobby maximum; the live
WebSocket closes after `SKYNET_WS_MAX_S`, 280 s, with code 1001 and clients
reconnect). Bodies are limited to about 4.5 MB, which is why clips go to object
storage through presigned URLs.

## Scaling and performance

The 10k msg/s with < 20 ms back-signal design point needs the standalone UDP
tier: a Vercel invocation per HTTPS batch adds tens of milliseconds and a
Redis round trip per pipeline stage, and cannot send UDP at all.

Measured with `bench/bench_ingest.py` in a 4-core Linux container (the
benchmark's senders and, for Redis, redis-server share those cores). The
scenario is deliberately dense: 200 nodes in four clusters, objects seen by
2-20 nodes (median ~6), about 40% of all packets complete a triangulation.

30 s measured after an 8 s warm-up. Back-signal latency is server-side
(datagram received to clip request sent) and end-to-end at the nodes (packet
that completed the match sent to clip request received); shed is telemetry
the server never processed; CPU is the server's (redis-server excluded).

| State, processes | offered msg/s | processed msg/s | shed | back-signal p50 / p99 ms | end-to-end p50 / p99 ms | CPU cores |
|---|---|---|---|---|---|---|
| memory, 1 | 3,105 | 3,105 | 0 % | 1.2 / 19 | 1.6 / 28 | 0.72 |
| memory, 1 | 4,068 | 4,068 | 0 % | 1.9 / 95 | 2.6 / 108 | 0.82 |
| memory, 1 | 6,069 | 5,978 | 1.5 % | 52 / 321 | 57 / 326 | 0.97 |
| memory, 1 | 10,191 | 6,313 | 38 % | 203 / 244 | 209 / 267 | 0.99 |
| redis, 1 | 10,191 | 3,104 | 70 % | 217 / 484 | 221 / 382 | 0.91 |
| redis, 3 workers | 6,069 | 6,087 | 0 % | 23 / 139 | 25 / 153 | 2.43 |
| redis, 3 workers | 10,191 | 8,260 | 19 % | 525 / 700 | 534 / 704 | 2.48 |

**The 10k msg/s target is not met on this hardware.** One memory-backend
process saturates at about 6k msg/s and keeps the back-signal p99 under
20 ms up to about 3k msg/s; three Redis workers absorb 6k msg/s without loss
and top out near 8.3k.

`bench/bench_inprocess.py` measures one process's matching capacity with no
network or sender processes competing for the CPU: about **7,900 msg/s
(127 µs per message)** on one 2.1 GHz Xeon core of this container. Run it on
the host you plan to use (for example the Mac Studio) to see whether a
single process reaches 10k msg/s there:

```bash
server/.venv/bin/python server/bench/bench_inprocess.py
```

* One process is CPU-bound in Python, mostly in matching. Within capacity
  the median back-signal (receipt to `sendto`) is a few milliseconds; the
  tail comes from queueing behind bursts and garbage-collection pauses.
* Beyond capacity the server sheds the oldest datagrams and keeps latency
  bounded instead of building a backlog.
* Scaling options, in order of preference:
  1. a faster core: the Mac Studio's cores are considerably faster than this
     container's (not measured here);
  2. **sharding by region**: nodes more than 2 x 80 miles apart never match,
     so independent standalone instances (memory backend) can each serve a
     region; nodes within 80 miles of a boundary report to both instances;
  3. `--workers N` with the Redis backend (Linux): `SO_REUSEPORT` spreads
     nodes over workers by source address, and the workers share detections
     through Redis. It costs about twice the CPU per message, so it pays off
     only with spare cores. macOS does not balance UDP across
     `SO_REUSEPORT` sockets, so there it gives no UDP scaling.

## Tests

```bash
server/.venv/bin/python -m pytest server/tests
```

The suite starts a private `redis-server` (port 6390) and a throwaway
PostgreSQL + PostGIS cluster (port 5440, run as the `postgres` user when the
tests run as root); tests needing them are skipped when the binaries are
missing. Covered: decode parity with the reference, every rejection reason
and replay handling, the state-backend contract on memory and real Redis,
matchmaking on real geometry (interpolation, gating, N-ray solutions,
inconsistent-ray rejection, search plans, view-cone pruning), track
kinematics and classification, UDP back-signals verified with
`decode_for_node`, every API endpoint on both backends (ingest, commands,
node auth, events paging, coverage including hand-checked cells, the clip
flow, CORS, WebSocket), volunteers against PostGIS, migrations, SigV4 against
AWS's published example and botocore output, and serving `api/index.py`.

## Not verified here

* **Vercel**: no deployment was possible from the build environment.
  `api/index.py` was imported and served with uvicorn from a fresh virtualenv
  built only from `requirements.txt` (vercel mode, Redis, WebSocket
  included), and the configuration follows the builder source and Vercel's
  examples, but the real build, the rewrite to `/api/index`, the
  `excludeFiles` glob, Upstash/Neon behaviour and WebSockets on Vercel are
  untested.
* **macOS**: the code avoids Linux-only calls (the UDP listener uses
  `loop.add_reader`, which the default kqueue loop and uvloop support;
  `SO_REUSEPORT` is only used for `--workers`), but it was not run on macOS.
* **Object storage**: presigned URLs match AWS's test vector and botocore
  byte-for-byte; no upload to a real bucket was made.
