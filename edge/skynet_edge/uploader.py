"""Clip uploads in answer to the server's ``clip_request`` back-signal.

Flow (docs/API.md, "Clip upload"):

1. ``ClipManager.request_clip`` produces an MP4 for ``[t_start, t_end]``:
   from a stored event clip that covers it, by joining an event still being
   recorded, or by protecting and exporting that window from the ring
   buffer.
2. ``POST upload_url`` (node-authenticated, empty body) returns the real
   upload target ``{"method", "url", "headers"}``: the server's own clip
   endpoint, or a presigned object-storage URL on Vercel deployments.
3. The file is sent with exactly that method, URL and headers. Node-auth
   headers are added only when the target is on the server itself; adding
   unknown headers to a presigned URL can invalidate it.
4. ``POST .../complete`` with size, SHA-256, content type, the true first and
   last frame times and the frame count.

Any failure restarts the sequence from step 2 after an exponential backoff
(1, 2, 4 ... 60 s), up to ``clips.upload_max_attempts`` times. Uploads run
on their own thread, one at a time, so they never compete with telemetry
for CPU; the network stack shares the link anyway.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import requests

from skynet_common.protocol import ClipRequest

from .clips import ClipManager, ClipResult
from .config import ClipsConfig, ServerConfig
from .uplink import NodeAuth, resolve_server_url

log = logging.getLogger(__name__)


@dataclass
class UploadOutcome:
    request_id: str
    ok: bool
    attempts: int
    bytes: int = 0
    error: str = ""


def complete_url_for(upload_url: str, request_id: str) -> str:
    """``.../clips/{id}/upload-target`` -> ``.../clips/{id}/complete`` (keeps any path prefix)."""
    suffix = "/upload-target"
    path = urlsplit(upload_url).path
    if path.endswith(suffix):
        return upload_url[: upload_url.rfind(suffix)] + "/complete"
    return urljoin(upload_url, f"/api/v1/clips/{request_id}/complete")


class ClipUploader:
    def __init__(self, auth: NodeAuth, clips: ClipManager, server: ServerConfig, cfg: ClipsConfig,
                 session: Optional[requests.Session] = None, sleep: Callable[[float], None] = time.sleep):
        self.auth = auth
        self.clips = clips
        self.server = server
        self.cfg = cfg
        self._session = session or requests.Session()
        self._sleep = sleep
        self._q: "queue.Queue[Optional[Tuple[ClipRequest, ClipResult]]]" = queue.Queue()
        self._pending = 0
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.outcomes: Dict[str, UploadOutcome] = {}

    # --- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="clip-upload", daemon=True)
        self._thread.start()

    def drain(self, timeout: float) -> bool:
        """Wait until every accepted request has been uploaded or has failed."""
        with self._idle:
            return self._idle.wait_for(lambda: self._pending == 0, timeout)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._q.put(None)
        if self._thread is not None:
            self._thread.join(timeout)

    # --- intake -----------------------------------------------------------------

    def submit(self, req: ClipRequest) -> None:
        """Handle a verified clip_request (called from the uplink's receive thread)."""
        with self._lock:
            self._pending += 1
        self.clips.request_clip(req.request_id, req.t_start_ns, req.t_end_ns, lambda res: self._q.put((req, res)))

    def _done(self, outcome: UploadOutcome) -> None:
        with self._idle:
            self.outcomes[outcome.request_id] = outcome
            self._pending -= 1
            self._idle.notify_all()

    # --- worker -------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            req, res = item
            if not res.ok or not res.mp4_path:
                log.error("clip %s unavailable: %s", req.request_id, res.error or "no video")
                self._done(UploadOutcome(req.request_id, False, 0, error=res.error or "no video"))
                continue
            self._done(self._upload_with_retries(req, res))

    def _upload_with_retries(self, req: ClipRequest, res: ClipResult) -> UploadOutcome:
        delay = 1.0
        error = ""
        for attempt in range(1, self.cfg.upload_max_attempts + 1):
            try:
                self.upload(req, res)
                log.info("clip %s uploaded (%d bytes, attempt %d)", req.request_id, res.bytes, attempt)
                return UploadOutcome(req.request_id, True, attempt, res.bytes)
            except (requests.RequestException, OSError, ValueError, KeyError) as exc:
                error = str(exc)
                log.warning("clip %s upload attempt %d failed: %s", req.request_id, attempt, exc)
            if attempt < self.cfg.upload_max_attempts and not self._stop.is_set():
                self._sleep(delay)
                delay = min(60.0, delay * 2.0)
        return UploadOutcome(req.request_id, False, self.cfg.upload_max_attempts, error=error)

    def _url(self, url: str, reference: Optional[str]) -> str:
        return resolve_server_url(url, reference, self.server.https_base_url, self.server.rewrite_loopback_urls)

    def _signed(self, method: str, url: str, body: bytes = b"", headers: Optional[Dict[str, str]] = None
                ) -> requests.Response:
        h = dict(headers or {})
        h.update(self.auth.headers(method, url, body))
        resp = self._session.request(method, url, data=body, headers=h, timeout=self.server.request_timeout_s,
                                     verify=self.server.verify_tls)
        resp.raise_for_status()
        return resp

    def upload(self, req: ClipRequest, res: ClipResult) -> None:
        """One attempt at the three-step upload; raises on any failure."""
        assert res.mp4_path is not None
        target_url = self._url(req.upload_url, None)
        target = self._signed("POST", target_url).json()
        method = str(target.get("method", "PUT")).upper()
        put_url = self._url(str(target["url"]), target_url)
        headers = {str(k): str(v) for k, v in (target.get("headers") or {}).items()}
        body = Path(res.mp4_path).read_bytes()  # clips are a few MB to a few tens of MB
        timeout = max(self.server.request_timeout_s, len(body) / 250_000.0)  # allow ~2 Mbit/s uplinks
        if urlsplit(put_url).netloc == urlsplit(target_url).netloc:
            headers.update(self.auth.headers(method, put_url, body))
        resp = self._session.request(method, put_url, data=body, headers=headers, timeout=timeout,
                                     verify=self.server.verify_tls)
        resp.raise_for_status()
        complete = {"bytes": len(body), "sha256": res.sha256, "content_type": "video/mp4",
                    "t_start_ns": res.first_ns, "t_end_ns": res.last_ns, "frames": res.frames}
        payload = json.dumps(complete, separators=(",", ":")).encode("utf-8")
        self._signed("POST", self._url(complete_url_for(req.upload_url, req.request_id), target_url), payload,
                     {"Content-Type": "application/json"})
