"""Uplink to the SKY-NET server: UDP datagrams or batched HTTPS NDJSON.

Both transports carry the same signed bytes (``encode_signed``), so the only
difference is delivery:

* ``UdpUplink``: one non-blocking ``sendto`` per message; a full socket
  buffer drops the message rather than stalling the pipeline. The same
  socket receives the server's ``clip_request`` back-signals (the server
  answers to the source address of the node's latest datagram; heartbeats
  every 10 s keep NAT and Tailscale mappings alive). The server host may be
  a name (Tailscale MagicDNS, e.g. ``mac-studio``) and is re-resolved
  periodically in a background thread so DNS never blocks a send. The
  socket is IPv6 dual-stack where available, so IPv4 and IPv6 server
  addresses both work without reopening it.
* ``HttpsUplink``: messages are queued and POSTed as NDJSON to
  ``{base}/api/v1/ingest`` every ``batch_interval_s``; pending commands come
  back in the response. When nothing has been sent for
  ``command_poll_interval_s`` the node polls
  ``GET /api/v1/nodes/{id}/commands`` (node-authenticated). Failed batches
  are retried with backoff until their messages are older than
  ``max_message_age_s`` (the server rejects stale data anyway).

Server messages are accepted only if ``decode_for_node`` verifies them with
this node's key; anything else is counted and ignored. ``seq`` starts at the
current Unix time in milliseconds and increments, so it stays monotonic
across restarts without persisting state.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import select
import socket
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from skynet_common.protocol import (
    MAX_BATCH_LINES,
    MAX_DATAGRAM_BYTES,
    ClipRequest,
    ProtocolError,
    decode_for_node,
    encode_signed,
    sign_http_request,
)

from .config import ServerConfig

log = logging.getLogger(__name__)

CommandHandler = Callable[[ClipRequest], None]
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


class NodeAuth:
    """Headers for node-authenticated HTTP requests (PROTOCOL.md, "Signing")."""

    def __init__(self, node_id: str, key: Optional[bytes], clock: Callable[[], int] = time.time_ns):
        self.node_id = node_id
        self.key = key
        self._clock = clock

    def headers(self, method: str, url: str, body: bytes = b"") -> Dict[str, str]:
        if self.key is None:
            return {}
        ts = self._clock()
        path = urlsplit(url).path or "/"
        return {
            "X-Skynet-Node": self.node_id,
            "X-Skynet-Timestamp": str(ts),
            "X-Skynet-Signature": sign_http_request(self.key, method, path, ts, body),
        }


def _is_loopback_host(host: Optional[str]) -> bool:
    if not host:
        return False
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def resolve_server_url(url: str, reference: Optional[str], base: Optional[str], rewrite_loopback: bool) -> str:
    """Make a server-supplied URL usable from this node.

    Relative URLs are resolved against ``reference`` (the URL they came
    from) or ``base``. If the result points at a loopback or unspecified
    address -- what a server without a public URL setting advertises -- and
    ``base`` is configured, its scheme and host are replaced by ``base``'s.
    The path is kept, so node-auth signatures (which cover only the path)
    stay valid.
    """
    anchor = reference or base
    if anchor:
        url = urljoin(anchor, url)
    parts = urlsplit(url)
    if rewrite_loopback and base and _is_loopback_host(parts.hostname):
        b = urlsplit(base)
        url = urlunsplit((b.scheme, b.netloc, parts.path, parts.query, parts.fragment))
    return url


class Uplink:
    """Sequence numbers, signing, size checks, statistics and command dispatch."""

    transport = "none"

    def __init__(self, node_id: str, key: Optional[bytes], on_command: Optional[CommandHandler] = None,
                 clock: Callable[[], int] = time.time_ns):
        self.node_id = node_id
        self.key = key
        self.on_command = on_command
        self._clock = clock
        self._seq_lock = threading.Lock()
        self._seq = time.time_ns() // 1_000_000
        self._seen_requests: "OrderedDict[str, None]" = OrderedDict()
        self.stats: Dict[str, int] = {"sent": 0, "dropped": 0, "errors": 0, "too_large": 0, "bytes": 0,
                                      "commands": 0, "bad_commands": 0}

    def encode(self, msg: Dict[str, Any]) -> Optional[bytes]:
        """Assign ``seq``, sign and size-check a message; None if it cannot be sent."""
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        data = encode_signed(dict(msg, seq=seq), self.key)
        if len(data) > MAX_DATAGRAM_BYTES:
            self.stats["too_large"] += 1
            log.error("%s message of %d bytes exceeds %d; not sent", msg.get("type"), len(data), MAX_DATAGRAM_BYTES)
            return None
        return data

    def send(self, msg: Dict[str, Any]) -> bool:
        data = self.encode(msg)
        if data is None:
            return False
        ok = self._send_bytes(data)
        if ok:
            self.stats["sent"] += 1
            self.stats["bytes"] += len(data)
        return ok

    def _send_bytes(self, data: bytes) -> bool:
        return True

    def start(self) -> None:
        """Start background threads (none for the null uplink)."""

    def stop(self, timeout: float = 5.0) -> None:
        """Flush and stop background threads."""

    def handle_command(self, raw: bytes) -> bool:
        """Verify and dispatch one server message; returns True if it was accepted."""
        if self.key is None:
            self.stats["bad_commands"] += 1
            log.warning("ignoring server message: node has no key to verify it")
            return False
        try:
            msg = decode_for_node(raw, self.key)
        except ProtocolError as exc:
            self.stats["bad_commands"] += 1
            log.warning("ignoring unverifiable server message: %s", exc)
            return False
        if not isinstance(msg, ClipRequest) or msg.node_id != self.node_id:
            self.stats["bad_commands"] += 1
            log.warning("ignoring server message %s for node %s", type(msg).__name__, getattr(msg, "node_id", "?"))
            return False
        if msg.request_id in self._seen_requests:
            return False  # delivered twice (e.g. UDP and polling): handle once
        self._seen_requests[msg.request_id] = None
        while len(self._seen_requests) > 512:
            self._seen_requests.popitem(last=False)
        self.stats["commands"] += 1
        log.info("clip_request %s for [%d, %d]", msg.request_id, msg.t_start_ns, msg.t_end_ns)
        if self.on_command is not None:
            self.on_command(msg)
        return True


class NullUplink(Uplink):
    """Encodes and signs (so the cost is measured) but sends nowhere; optionally records."""

    def __init__(self, node_id: str, key: Optional[bytes], sink: Optional[Callable[[bytes], None]] = None):
        super().__init__(node_id, key)
        self._sink = sink

    def _send_bytes(self, data: bytes) -> bool:
        if self._sink is not None:
            self._sink(data)
        return True


class UdpUplink(Uplink):
    transport = "udp"

    def __init__(self, cfg: ServerConfig, node_id: str, key: Optional[bytes],
                 on_command: Optional[CommandHandler] = None, clock: Callable[[], int] = time.time_ns):
        super().__init__(node_id, key, on_command, clock)
        self.cfg = cfg
        self._sock, self._dual_stack = self._make_socket()
        self._addr: Optional[Tuple[Any, ...]] = None
        self._addr_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self.stats.update({"unresolved": 0, "resolve_errors": 0})

    @staticmethod
    def _make_socket() -> Tuple[socket.socket, bool]:
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            dual = True
        except OSError:  # IPv6 disabled on this host
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            dual = False
        sock.setblocking(False)
        return sock, dual

    @property
    def server_addr(self) -> Optional[Tuple[Any, ...]]:
        with self._addr_lock:
            return self._addr

    def resolve(self) -> bool:
        """Look up the server; returns True if an address was found."""
        try:
            infos = socket.getaddrinfo(self.cfg.udp_host, self.cfg.udp_port, type=socket.SOCK_DGRAM)
        except OSError as exc:
            self.stats["resolve_errors"] += 1
            log.warning("cannot resolve server %s: %s", self.cfg.udp_host, exc)
            return False
        v4 = [i[4] for i in infos if i[0] == socket.AF_INET]
        v6 = [i[4] for i in infos if i[0] == socket.AF_INET6]
        if self._dual_stack:
            addr = ((f"::ffff:{v4[0][0]}", v4[0][1], 0, 0) if v4 else v6[0]) if (v4 or v6) else None
        else:
            addr = v4[0] if v4 else None
        if addr is None:
            log.warning("server %s has no usable address", self.cfg.udp_host)
            return False
        with self._addr_lock:
            changed = addr != self._addr
            self._addr = addr
        if changed:
            log.info("server %s:%d -> %s", self.cfg.udp_host, self.cfg.udp_port, addr[0])
        return True

    def start(self) -> None:
        self.resolve()
        self._threads = [threading.Thread(target=self._resolver, name="udp-resolve", daemon=True),
                         threading.Thread(target=self._receiver, name="udp-recv", daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout)
        self._sock.close()

    def _resolver(self) -> None:
        while not self._stop.is_set():
            ok = self.resolve()
            # Retry quickly while unresolved (e.g. tailscaled still starting at boot).
            self._stop.wait(self.cfg.udp_resolve_interval_s if ok else min(5.0, self.cfg.udp_resolve_interval_s))

    def _receiver(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._sock], [], [], 0.25)
                if not ready:
                    continue
                data, _ = self._sock.recvfrom(4096)
            except (OSError, ValueError):
                if self._stop.is_set():
                    return
                continue
            try:
                self.handle_command(data)
            except Exception:  # a bad callback must not kill the receiver
                log.exception("clip_request handler failed")

    def _send_bytes(self, data: bytes) -> bool:
        addr = self.server_addr
        if addr is None:
            self.stats["unresolved"] += 1
            return False
        try:
            self._sock.sendto(data, addr)
            return True
        except BlockingIOError:
            self.stats["dropped"] += 1
        except OSError as exc:  # network down, no route (Tailscale not up yet)
            self.stats["errors"] += 1
            log.debug("UDP send failed: %s", exc)
        return False


class HttpsUplink(Uplink):
    transport = "https"
    MAX_QUEUE = 20000

    def __init__(self, cfg: ServerConfig, node_id: str, key: Optional[bytes],
                 on_command: Optional[CommandHandler] = None, clock: Callable[[], int] = time.time_ns,
                 session: Optional[requests.Session] = None):
        super().__init__(node_id, key, on_command, clock)
        assert cfg.https_base_url
        self.cfg = cfg
        self.base = cfg.https_base_url.rstrip("/")
        self.ingest_url = f"{self.base}/api/v1/ingest"
        self.commands_url = f"{self.base}/api/v1/nodes/{node_id}/commands"
        self.auth = NodeAuth(node_id, key, clock)
        self._session = session or requests.Session()
        self._q: Deque[Tuple[float, bytes]] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_contact = 0.0
        self.stats.update({"batches": 0, "rejected": 0, "expired": 0, "polls": 0})

    def _send_bytes(self, data: bytes) -> bool:
        with self._lock:
            if len(self._q) >= self.MAX_QUEUE:
                self._q.popleft()
                self.stats["dropped"] += 1
            self._q.append((time.monotonic(), data))
        return True

    def pending(self) -> int:
        with self._lock:
            return len(self._q)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="https-uplink", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while self.pending() and time.monotonic() < deadline and self._thread is not None:
            time.sleep(0.02)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(max(0.1, deadline - time.monotonic()))

    def _take_batch(self) -> List[Tuple[float, bytes]]:
        cutoff = time.monotonic() - self.cfg.max_message_age_s
        batch: List[Tuple[float, bytes]] = []
        with self._lock:
            while self._q and len(batch) < MAX_BATCH_LINES:
                item = self._q.popleft()
                if item[0] < cutoff:
                    self.stats["expired"] += 1
                    continue
                batch.append(item)
        return batch

    def _requeue(self, batch: List[Tuple[float, bytes]]) -> None:
        with self._lock:
            self._q.extendleft(reversed(batch))

    def _run(self) -> None:
        backoff = 0.0
        while not self._stop.wait(max(self.cfg.batch_interval_s, backoff)):
            batch = self._take_batch()
            try:
                if batch:
                    self._post(batch)
                elif time.monotonic() - self._last_contact >= self.cfg.command_poll_interval_s:
                    self.poll_commands()
                backoff = 0.0
            except (requests.RequestException, ValueError) as exc:
                self.stats["errors"] += 1
                backoff = min(5.0, max(0.25, backoff * 2.0))
                if batch:
                    self._requeue(batch)
                log.warning("HTTPS uplink: %s (retry in %.2f s)", exc, backoff)

    def _post(self, batch: List[Tuple[float, bytes]]) -> None:
        body = b"\n".join(data for _, data in batch) + b"\n"
        resp = self._session.post(self.ingest_url, data=body, headers={"Content-Type": "application/x-ndjson"},
                                  timeout=self.cfg.request_timeout_s, verify=self.cfg.verify_tls)
        if 400 <= resp.status_code < 500:
            self.stats["errors"] += 1
            log.error("ingest rejected the whole batch: HTTP %d %s", resp.status_code, resp.text[:200])
            return  # retrying an invalid request cannot succeed
        resp.raise_for_status()
        self._last_contact = time.monotonic()
        self.stats["batches"] += 1
        reply = resp.json()
        rejected = reply.get("rejected") or []
        if rejected:
            self.stats["rejected"] += len(rejected)
            log.warning("server rejected %d of %d lines, e.g. %s", len(rejected), len(batch), rejected[0])
        self._dispatch(reply.get("commands") or [])

    def poll_commands(self) -> None:
        headers = self.auth.headers("GET", self.commands_url)
        resp = self._session.get(self.commands_url, headers=headers, timeout=self.cfg.request_timeout_s,
                                 verify=self.cfg.verify_tls)
        resp.raise_for_status()
        self._last_contact = time.monotonic()
        self.stats["polls"] += 1
        self._dispatch(resp.json().get("commands") or [])

    def _dispatch(self, commands: List[Any]) -> None:
        for cmd in commands:
            raw = cmd.encode("utf-8") if isinstance(cmd, str) else json.dumps(cmd).encode("utf-8")
            try:
                self.handle_command(raw)
            except Exception:
                log.exception("clip_request handler failed")


def make_uplink(cfg: ServerConfig, node_id: str, key: Optional[bytes],
                on_command: Optional[CommandHandler] = None) -> Uplink:
    if cfg.transport == "udp":
        return UdpUplink(cfg, node_id, key, on_command)
    if cfg.transport == "https":
        return HttpsUplink(cfg, node_id, key, on_command)
    return NullUplink(node_id, key)
