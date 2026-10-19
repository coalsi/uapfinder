"""The HTTP API (docs/API.md) and the process runtime behind it.

``create_app()`` builds the same FastAPI application for both deployments:

* standalone: ``skynet-server serve`` runs it under uvicorn, and the lifespan
  also starts the UDP listener and the batched database writer on the same
  event loop;
* vercel: ``api/index.py`` exposes it as the Python Function's ASGI ``app``.
  State lives in Redis, database writes are awaited before responding (a
  Function may be frozen right after it responds) and ``WS /api/v1/live``
  forwards the Redis event stream until shortly before the Function's
  maximum duration.

Every route depends on ``Runtime.ensure_started`` as well, so the app also
works under servers that skip the ASGI lifespan.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import hmac
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import orjson
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from skynet_common.protocol import MAX_BATCH_LINES, MAX_DATAGRAM_BYTES, sign_http_digest

from . import __version__
from .config import Settings
from .coverage import CoverageError, camera_nodes, compute_coverage
from .engine import Engine, Inbound
from .live import EventHub, stream_events
from .models import ClipRecord, Commit
from .persistence import BatchWriter, Database, DbBatch
from .state import create_state
from .state.base import EVENT_ID_RE
from .stats import Stats, prometheus_text
from .storage import LocalStorage, StorageError, create_storage
from .volunteers import MAX_BODY_BYTES, MIN_CELL_COUNT, RATE_WINDOW_S, VolunteerIn, is_honeypot, to_csv, to_json_row

log = logging.getLogger("skynet.app")

NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")
CLASSIFICATIONS = ("unknown", "ballistic", "hypersonic", "high_g", "stationary")
ACTIVE_TRACK_NS = 30_000_000_000
_INGEST_MAX_BYTES = MAX_BATCH_LINES * (MAX_DATAGRAM_BYTES + 2)
_AUTH_BODY_MAX = 64 * 1024
_HOUR_NS = 3600 * 1_000_000_000


class ORJSON(Response):
    """JSON responses via orjson (handles the 19-digit ns integers fast)."""

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return orjson.dumps(content)


class Runtime:
    """Everything one server process owns: state, engine, database, storage, UDP."""

    def __init__(self, settings: Settings):
        self.settings = settings
        shared = settings.state_backend == "redis"
        # Vercel instances flush their stats with every request; long-lived
        # processes once a second.
        self.stats = Stats(shared=shared, flush_interval_s=0.0 if settings.mode == "vercel" else 1.0)
        self.state = create_state(settings)
        self.hub = EventHub()
        self.db: Optional[Database] = None
        self.writer: Optional[BatchWriter] = None
        if settings.database_url:
            self.db = Database(settings.database_url, settings.db_statement_cache, settings.store_detections)
            if settings.mode == "standalone":
                self.writer = BatchWriter(self.db, settings.db_batch_ms)
        self.engine = Engine(settings, self.state, self.stats, self.hub, self.db, self.writer)
        self.storage = create_storage(settings)
        self.udp: Any = None
        self._started = False
        self._lock = asyncio.Lock()

    async def ensure_started(self) -> None:
        if self._started:
            return
        async with self._lock:
            if self._started:
                return
            await self.state.start()
            if self.db is not None:
                await self.db.start()
                await self._restore_nodes()
            if self.writer is not None:
                self.writer.start()
            if self.settings.mode == "standalone" and not self.settings.public_url:
                log.warning("SKYNET_PUBLIC_URL is not set: clip upload URLs sent to UDP nodes will use %s",
                            self.engine.base_url())
            # Everything allocated so far (modules, numpy, configuration) lives
            # for the whole process; keeping it out of every full collection
            # shortens the pauses that delay UDP processing.
            gc.freeze()
            if self.settings.udp_enabled:
                from .udp import UdpListener

                s = self.settings
                assert s.udp_port is not None
                # Several workers share the port; Linux spreads datagrams over
                # them by source address, so each node sticks to one worker.
                self.udp = UdpListener(self.engine, s.udp_host, s.udp_port, s.udp_rcvbuf, s.udp_batch,
                                       reuse_port=s.workers > 1)
                await self.udp.start()
            self._started = True

    async def _restore_nodes(self) -> None:
        """The memory backend starts empty; reload node labels and geometry."""
        if self.state.name != "memory" or self.db is None:
            return
        try:
            records = await self.db.list_nodes()
        except Exception:  # noqa: BLE001 - a database outage must not prevent start-up
            log.exception("could not restore nodes from the database")
            return
        for node in records:
            self.state.nodes.setdefault(node.node_id, node)  # type: ignore[attr-defined]

    async def stop(self) -> None:
        if not self._started:
            return
        if self.udp is not None:
            await self.udp.stop()
        if self.writer is not None:
            await self.writer.stop()
        if self.db is not None:
            await self.db.close()
        await self.state.close()
        self._started = False


# --- request bodies ------------------------------------------------------------------


class NodeRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Optional[str] = Field(default=None, max_length=200)
    node_id: Optional[str] = Field(default=None, pattern=NODE_ID_RE.pattern)


class ClipComplete(BaseModel):
    model_config = ConfigDict(extra="ignore")
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    content_type: str = Field(default="video/mp4", max_length=100)
    t_start_ns: Optional[int] = None
    t_end_ns: Optional[int] = None
    frames: Optional[int] = Field(default=None, ge=0)


def _validation_error(exc: ValidationError) -> RequestValidationError:
    return RequestValidationError(exc.errors(include_url=False, include_context=False, include_input=False))


async def read_body(request: Request, limit: int) -> bytes:
    """The request body, or 413 once it exceeds ``limit`` bytes."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"request body exceeds {limit} bytes")
    chunks: List[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"request body exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    rt = Runtime(settings)
    state, engine = rt.state, rt.engine

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await rt.ensure_started()
        try:
            yield
        finally:
            await rt.stop()

    app = FastAPI(title="SKY-NET server", version=__version__, lifespan=lifespan, default_response_class=ORJSON,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.state.rt = rt
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Skynet-Node", "X-Skynet-Timestamp",
                           "X-Skynet-Signature"],
            max_age=3600,
        )

    async def started() -> None:
        await rt.ensure_started()

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(started)])

    # --- helpers ---------------------------------------------------------------

    def base_url(request: Request) -> str:
        return engine.base_url(str(request.base_url))

    def client_ip(request: Request) -> str:
        if settings.trust_proxy:
            forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
            if forwarded:
                return forwarded[-1]  # appended by the one trusted proxy in front of us
        return request.client.host if request.client else "unknown"

    def require_admin(request: Request) -> None:
        if not settings.admin_token:
            raise HTTPException(503, "admin API disabled (set SKYNET_ADMIN_TOKEN)")
        # tailscaled marks every request that arrived through Tailscale Funnel
        # (the public internet) and strips the header from client input, so the
        # admin API stays reachable only from the tailnet or the host itself.
        if request.headers.get("tailscale-funnel-request"):
            raise HTTPException(403, "admin API is not available through Tailscale Funnel; use the tailnet address")
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip().encode(), settings.admin_token.encode()):
            raise HTTPException(401, "invalid admin token", headers={"WWW-Authenticate": "Bearer"})

    def auth_headers(request: Request) -> Tuple[str, int, str]:
        """Node id, timestamp and signature of a node-authenticated request."""
        node_id = request.headers.get("x-skynet-node", "")
        ts_raw = request.headers.get("x-skynet-timestamp", "")
        signature = request.headers.get("x-skynet-signature", "").lower()
        if not NODE_ID_RE.match(node_id) or not ts_raw.isdigit() or not SIGNATURE_RE.match(signature):
            raise HTTPException(401, "missing or malformed X-Skynet-Node/-Timestamp/-Signature headers")
        if settings.master_key is None:
            raise HTTPException(503, "server has no master key configured")
        ts = int(ts_raw)
        if abs(time.time_ns() - ts) > settings.http_auth_window_ns:
            raise HTTPException(401, "request timestamp outside the allowed window")
        return node_id, ts, signature

    async def check_signature(request: Request, node_id: str, ts: int, signature: str, body_sha256: str) -> None:
        expected = sign_http_digest(engine.decoder.node_key(node_id), request.method, request.url.path, ts,
                                  body_sha256)
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(401, "bad request signature")
        # A captured request must not be replayable inside the time window.
        ttl_ms = 2 * settings.http_auth_window_ns // 1_000_000
        if not (await state.claim(["h:" + signature], ttl_ms))[0]:
            raise HTTPException(401, "replayed request")

    async def node_auth(request: Request) -> Tuple[str, bytes]:
        """Verify X-Skynet-* request signing (PROTOCOL.md); returns (node id, body)."""
        node_id, ts, signature = auth_headers(request)
        body = await read_body(request, _AUTH_BODY_MAX)
        await check_signature(request, node_id, ts, signature, hashlib.sha256(body).hexdigest())
        return node_id, body

    async def find_clip(request_id: str) -> ClipRecord:
        clip = await state.get_clip(request_id)
        if clip is None and rt.db is not None:
            clip = await rt.db.get_clip(request_id)
        if clip is None:
            raise HTTPException(404, "unknown clip request")
        return clip

    def clip_public(clip: ClipRecord, request: Request) -> Dict[str, Any]:
        url = rt.storage.download_url(clip, base_url(request)) if clip.status == "uploaded" else None
        return {"request_id": clip.request_id, "event_id": clip.event_id, "track_id": clip.track_id,
                "node_id": clip.node_id, "status": clip.status, "download_url": url, "t_start_ns": clip.t_start_ns,
                "t_end_ns": clip.t_end_ns, "bytes": clip.bytes}

    def require_db() -> Database:
        if rt.db is None:
            raise HTTPException(503, "sign-up unavailable")
        return rt.db

    # --- status ------------------------------------------------------------------

    @api.get("/health")
    async def health() -> Dict[str, Any]:
        db_status = "disabled"
        if rt.db is not None:
            db_status = "ok" if await rt.db.ping() else "error"
        return {
            "status": "ok",
            "version": __version__,
            "mode": settings.mode,
            "state_backend": state.name,
            "state": "ok" if await state.ping() else "error",
            "db": db_status,
            "udp_port": rt.udp.port if rt.udp is not None else None,
            "time_ns": time.time_ns(),
        }

    async def stats_snapshot() -> Dict[str, Any]:
        if rt.stats.shared:  # include what this process has not flushed yet
            delta = rt.stats.drain()
            if not delta.is_empty():
                await state.commit(Commit(), settings.tracks.link_ttl_ms, delta)
        snap = await state.shared_stats() or rt.stats.snapshot()
        now = time.time_ns()
        snap["active_nodes"] = await state.count_active_nodes(now - settings.node_active_ns)
        snap["active_tracks"] = await state.count_active_tracks(now - ACTIVE_TRACK_NS)
        if rt.writer is not None:
            snap.setdefault("counters", {})["db_rows_dropped"] = rt.writer.dropped_rows
        return snap

    @api.get("/stats")
    async def stats() -> Dict[str, Any]:
        return await stats_snapshot()

    async def metrics() -> PlainTextResponse:
        await rt.ensure_started()
        return PlainTextResponse(prometheus_text(await stats_snapshot()), media_type="text/plain; version=0.0.4")

    app.add_api_route("/metrics", metrics, methods=["GET"], include_in_schema=False)
    api.add_api_route("/metrics", metrics, methods=["GET"])

    # --- nodes -------------------------------------------------------------------

    @api.post("/nodes", status_code=201)
    async def register_node(request: Request) -> Dict[str, Any]:
        require_admin(request)
        if settings.master_key is None:
            raise HTTPException(503, "server has no master key configured (SKYNET_MASTER_KEY)")
        raw = await read_body(request, 4096)
        try:
            body = NodeRegistration.model_validate_json(raw or b"{}")
        except ValidationError as exc:
            raise _validation_error(exc) from None
        node_id = body.node_id or str(uuid.uuid4())
        node, _ = await state.register_node(node_id, body.label, time.time_ns())
        await engine.emit([{"type": "node", "t_ns": time.time_ns(), "data": engine.public_node(node)}])
        await engine.persist(DbBatch(nodes={node_id: node}))
        udp = settings.udp_public_endpoint
        if udp is None and rt.udp is not None:
            udp = f"{request.url.hostname}:{rt.udp.port}"
        return {"node_id": node_id, "node_key": engine.decoder.node_key(node_id).hex(),
                "ingest_url": f"{base_url(request)}/api/v1/ingest", "udp_endpoint": udp}

    @api.get("/nodes")
    async def list_nodes(online_only: bool = False) -> Dict[str, Any]:
        now = time.time_ns()
        nodes = [engine.public_node(n, now) for n in await state.list_nodes()]
        return {"nodes": [n for n in nodes if n["online"]] if online_only else nodes}

    @api.post("/ingest")
    async def ingest(request: Request) -> Dict[str, Any]:
        body = await read_body(request, _INGEST_MAX_BYTES)
        lines = [(i + 1, line.strip()) for i, line in enumerate(body.split(b"\n"))]
        lines = [(n, line) for n, line in lines if line]
        if len(lines) > MAX_BATCH_LINES:
            raise HTTPException(413, f"at most {MAX_BATCH_LINES} lines per request")
        now, perf = time.time_ns(), time.perf_counter_ns()
        out = await engine.process([Inbound(line, now, perf) for _, line in lines], str(request.base_url))
        pending = await state.pop_commands(sorted(out.node_ids)) if out.node_ids else {}
        return {
            "accepted": out.accepted,
            "rejected": [{"line": n, "reason": r} for (n, _), r in zip(lines, out.reasons) if r is not None],
            "commands": [c for node_id in sorted(pending) for c in pending[node_id]],
        }

    @api.get("/nodes/{node_id}/commands")
    async def commands(node_id: str, request: Request) -> Dict[str, Any]:
        if (await node_auth(request))[0] != node_id:
            raise HTTPException(403, "a node may only fetch its own commands")
        return {"commands": (await state.pop_commands([node_id])).get(node_id, [])}

    # --- tracks and events ---------------------------------------------------------------

    @api.get("/tracks")
    async def list_tracks(since_ns: Optional[int] = None, limit: int = Query(100, ge=1, le=1000),
                          classification: Optional[str] = None) -> Dict[str, Any]:
        if classification is not None and classification not in CLASSIFICATIONS:
            raise HTTPException(422, f"classification must be one of {', '.join(CLASSIFICATIONS)}")
        since = since_ns if since_ns is not None else time.time_ns() - _HOUR_NS
        tracks = {t.track_id: t.public() for t in await state.list_tracks(since, limit, classification)}
        if rt.db is not None:  # history beyond the state backend's retention, or before a restart
            try:
                for t in await rt.db.list_tracks(since, limit, classification):
                    tracks.setdefault(t["track_id"], t)
            except Exception:  # noqa: BLE001 - live data still answers without the database
                log.exception("track history query failed")
        ordered = sorted(tracks.values(), key=lambda t: t["last_ns"], reverse=True)[:limit]
        points = await state.get_track_points([t["track_id"] for t in ordered], 200)
        for t in ordered:
            t["points"] = points.get(t["track_id"], [])
            if not t["points"] and rt.db is not None:
                try:
                    t["points"] = await rt.db.track_points(t["track_id"], 200)
                except Exception:  # noqa: BLE001
                    log.exception("track history query failed")
        return {"tracks": ordered}

    @api.get("/tracks/{track_id}")
    async def get_track(track_id: str, request: Request) -> Dict[str, Any]:
        live = (await state.get_tracks([track_id])).get(track_id)
        track = live.public() if live is not None else None
        points = (await state.get_track_points([track_id], settings.tracks.max_points)).get(track_id, [])
        clips = {c.request_id: c for c in await state.track_clips(track_id)}
        if rt.db is not None:
            try:
                if track is None:
                    track = await rt.db.get_track(track_id)
                stored = await rt.db.track_points(track_id, 1_000_000)
                if stored:
                    newer = [p for p in points if p["t_ns"] > stored[-1]["t_ns"]]  # not flushed to the DB yet
                    points = stored + newer
                for c in await rt.db.track_clips(track_id):
                    clips.setdefault(c.request_id, c)
            except Exception:  # noqa: BLE001 - live data still answers without the database
                log.exception("track history query failed")
        if track is None:
            raise HTTPException(404, "unknown track")
        track["points"] = points
        track["clips"] = [{k: v for k, v in clip_public(c, request).items()
                           if k in ("request_id", "node_id", "status", "download_url")}
                          for c in sorted(clips.values(), key=lambda c: c.issued_ns)]
        return track

    def check_cursor(cursor: Optional[str]) -> None:
        if cursor is not None and not EVENT_ID_RE.match(cursor):
            raise HTTPException(400, "after must be an event id such as 1782345600012-0")

    @api.get("/events")
    async def events(after: Optional[str] = None, limit: int = Query(200, ge=1, le=1000)) -> Dict[str, Any]:
        check_cursor(after)
        found = await state.events_after(after, limit)
        cursor = found[-1]["id"] if found else (after or await state.last_event_id())
        return {"events": found, "cursor": cursor}

    @app.websocket("/api/v1/live")
    async def live(ws: WebSocket) -> None:
        await rt.ensure_started()
        subscription = None
        if state.name == "memory":
            subscription = rt.hub.subscribe()
            source: AsyncIterator[Dict[str, Any]] = subscription
        else:  # every instance's events are in the shared stream
            source = stream_events(state, settings.live_poll_ms / 1000.0, await state.last_event_id() or "0-0")
        await ws.accept()

        async def pump() -> None:
            async for event in source:
                await ws.send_text(orjson.dumps(event).decode())

        async def until_closed() -> None:
            while (await ws.receive())["type"] != "websocket.disconnect":
                pass

        tasks = [asyncio.ensure_future(pump()), asyncio.ensure_future(until_closed())]
        try:
            done, _ = await asyncio.wait(tasks, timeout=settings.ws_max_s, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if subscription is not None:
                subscription.close()
            for task in tasks:
                task.cancel()
            # asyncio.wait rather than gather: if this handler is itself being
            # cancelled meanwhile, gather would re-raise the child's
            # CancelledError and drop the canceller's message, which anyio
            # cancel scopes (e.g. the TestClient's) use to recognise their own.
            await asyncio.wait(tasks)
            for task in tasks:
                if not task.cancelled():
                    task.exception()  # retrieved: a failed send just ends the session
        if not done or tasks[0] in done:
            # Duration limit (Vercel) or a send failure: ask the client to reconnect.
            try:
                await ws.close(code=1001)
            except RuntimeError:
                pass

    # --- coverage ------------------------------------------------------------------------

    @api.get("/coverage")
    async def coverage(min_lat: float, min_lon: float, max_lat: float, max_lon: float, cell_km: float = 10.0,
                       ref_alt_m: float = 3000.0, min_elevation_deg: float = 10.0,
                       min_parallax_deg: float = 5.0) -> Dict[str, Any]:
        nodes = camera_nodes(await state.list_nodes(), time.time_ns(), settings.node_active_ns,
                             settings.public_location_decimals)
        try:
            # CPU-bound: keep the event loop (and the UDP back-signal path) free.
            return await asyncio.to_thread(compute_coverage, nodes, min_lat, min_lon, max_lat, max_lon, cell_km,
                                           ref_alt_m, min_elevation_deg, min_parallax_deg, settings.match.radius_m)
        except CoverageError as exc:
            raise HTTPException(400, str(exc)) from None

    # --- clips ---------------------------------------------------------------------------

    @api.post("/clips/{request_id}/upload-target")
    async def upload_target(request_id: str, request: Request) -> Dict[str, Any]:
        node_id, _ = await node_auth(request)
        clip = await find_clip(request_id)
        if clip.node_id != node_id:
            raise HTTPException(403, "this clip was requested from another node")
        try:
            return rt.storage.upload_target(clip, base_url(request))
        except StorageError as exc:
            raise HTTPException(503, str(exc)) from None

    @api.put("/clips/{request_id}/data")
    async def upload_data(request_id: str, request: Request) -> Dict[str, Any]:
        """Local clip storage. Node-authenticated like every node request; the
        signature covers the body's SHA-256, so it is checked after the
        upload has streamed to a temporary file and before the file is kept."""
        storage = rt.storage
        if not isinstance(storage, LocalStorage):
            raise HTTPException(404, "uploads go to object storage on this deployment")
        node_id, ts, signature = auth_headers(request)
        clip = await find_clip(request_id)
        if clip.node_id != node_id:
            raise HTTPException(403, "this clip was requested from another node")
        try:
            size, digest, tmp = await storage.receive(clip, request.stream())
        except StorageError as exc:
            raise HTTPException(413, str(exc)) from None
        try:
            await check_signature(request, node_id, ts, signature, digest)
        except HTTPException:
            storage.discard(tmp)
            raise
        await storage.accept(tmp, clip)
        clip.bytes, clip.sha256 = size, digest
        await state.put_clip(clip)
        return {"bytes": size, "sha256": digest}

    @api.post("/clips/{request_id}/complete")
    async def complete(request_id: str, request: Request) -> Dict[str, Any]:
        node_id, body = await node_auth(request)
        try:
            info = ClipComplete.model_validate_json(body)
        except ValidationError as exc:
            raise _validation_error(exc) from None
        clip = await find_clip(request_id)
        if clip.node_id != node_id:
            raise HTTPException(403, "this clip was requested from another node")
        if clip.status == "uploaded":
            return {"status": "uploaded"}
        try:
            await rt.storage.verify(clip, info.bytes, info.sha256)
        except StorageError as exc:
            raise HTTPException(409, str(exc)) from None
        clip.status = "uploaded"
        clip.bytes, clip.sha256 = info.bytes, info.sha256.lower()
        clip.content_type, clip.frames, clip.uploaded_ns = info.content_type, info.frames, time.time_ns()
        clip.t_start_ns = info.t_start_ns or clip.t_start_ns
        clip.t_end_ns = info.t_end_ns or clip.t_end_ns
        clip.storage_key = f"{clip.track_id}/{clip.request_id}.mp4"
        await state.put_clip(clip)
        await engine.persist(DbBatch(clips={clip.request_id: clip}))
        public = clip_public(clip, request)
        await engine.emit([{"type": "clip", "t_ns": clip.uploaded_ns, "data": {
            k: public[k] for k in ("request_id", "event_id", "track_id", "node_id", "status", "download_url")}}])
        return {"status": "uploaded"}

    @api.get("/clips/{request_id}")
    async def get_clip(request_id: str, request: Request) -> Dict[str, Any]:
        return clip_public(await find_clip(request_id), request)

    @api.get("/clips/{request_id}/download")
    async def download(request_id: str) -> FileResponse:
        clip = await find_clip(request_id)
        storage = rt.storage
        if clip.status != "uploaded" or not isinstance(storage, LocalStorage) or not storage.path(clip).exists():
            raise HTTPException(404, "clip not available here")
        return FileResponse(storage.path(clip), media_type=clip.content_type or "video/mp4",
                            filename=f"{clip.request_id}.mp4")

    # --- volunteers ----------------------------------------------------------------------

    @api.post("/volunteers", status_code=201)
    async def volunteer_signup(request: Request) -> Dict[str, str]:
        db = require_db()
        if not await state.rate_limit("vol:" + client_ip(request), settings.volunteer_rate_per_hour, RATE_WINDOW_S):
            raise HTTPException(429, "too many sign-ups from this address; try again later")
        raw = await read_body(request, MAX_BODY_BYTES)
        try:
            data = orjson.loads(raw)
        except orjson.JSONDecodeError:
            raise RequestValidationError([{"type": "json_invalid", "loc": ("body",), "msg": "body is not JSON"}]) \
                from None
        if is_honeypot(data):
            return {"status": "received"}
        try:
            record = VolunteerIn.model_validate(data).record()
        except ValidationError as exc:
            raise _validation_error(exc) from None
        try:
            await db.upsert_volunteer(record)
        except Exception:  # noqa: BLE001 - report an outage, never a stack trace
            log.exception("volunteer sign-up could not be stored")
            raise HTTPException(503, "sign-up unavailable") from None
        return {"status": "received"}

    @api.get("/volunteers/summary")
    async def volunteer_summary() -> Dict[str, Any]:
        return await require_db().volunteer_summary(MIN_CELL_COUNT)

    @api.get("/volunteers")
    async def list_volunteers(request: Request, format: str = Query("json", pattern="^(json|csv)$")) -> Any:
        require_admin(request)
        rows = await require_db().list_volunteers()
        if format == "csv":
            return Response(to_csv(rows), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": 'attachment; filename="volunteers.csv"'})
        return {"volunteers": [to_json_row(r) for r in rows]}

    @api.delete("/volunteers/{volunteer_id}", status_code=204)
    async def delete_volunteer(volunteer_id: int, request: Request) -> Response:
        require_admin(request)
        if not await require_db().delete_volunteer(volunteer_id):
            raise HTTPException(404, "unknown volunteer")
        return Response(status_code=204)

    app.include_router(api)
    return app
