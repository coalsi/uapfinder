"""A tiny stand-in for the SKY-NET server: UDP ingest + the HTTP endpoints a node uses.

It verifies everything the real server verifies on node traffic (message
signatures via ``decode_message`` and node-auth headers via
``verify_http_request``) so the tests check the node against the protocol,
not against itself.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from skynet_common.protocol import (ProtocolError, decode_message, derive_node_key, encode_signed,
                                    verify_http_request)

UDP_PORTS = range(9200, 9300)
HTTP_PORTS = range(8200, 8300)


def bind_free(sock: socket.socket, ports: range, host: str = "127.0.0.1") -> int:
    """Bind ``sock`` to the first free port of the range reserved for these tests."""
    for port in ports:
        try:
            sock.bind((host, port))
            return port
        except OSError:
            continue
    raise RuntimeError(f"no free port in {ports}")


def clip_request(master: bytes, node_id: str, request_id: str, t_start_ns: int, t_end_ns: int,
                 upload_url: str, sign_with: Optional[bytes] = None) -> bytes:
    key = derive_node_key(master, node_id) if sign_with is None else sign_with
    return encode_signed({"type": "clip_request", "request_id": request_id, "event_id": f"ev-{request_id}",
                          "node_id": node_id, "t_start_ns": t_start_ns, "t_end_ns": t_end_ns,
                          "upload_url": upload_url, "issued_ns": time.time_ns()}, key)


class FakeUdpServer:
    def __init__(self, master: bytes):
        self.master = master
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.port = bind_free(self.sock, UDP_PORTS)
        self.sock.settimeout(0.2)
        self.messages: List[Any] = []
        self.rejected: List[str] = []
        self.peers: List[Tuple[str, int]] = []
        self.on_message: Optional[Callable[[Any, Tuple[str, int]], None]] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, peer = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                msg = decode_message(data, self.master)
            except ProtocolError as exc:
                self.rejected.append(exc.reason)
                continue
            self.messages.append(msg)
            self.peers.append(peer)
            if self.on_message is not None:
                self.on_message(msg, peer)

    def send(self, data: bytes, peer: Tuple[str, int]) -> None:
        self.sock.sendto(data, peer)

    def of_type(self, kind: str) -> List[Any]:
        return [m for m in list(self.messages) if m.type == kind]

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self.sock.close()


class FakeHttpServer:
    """Ingest, command polling and the three-step clip upload."""

    def __init__(self, master: bytes, put_url_style: str = "relative"):
        self.master = master
        self.put_url_style = put_url_style
        self.ingested: List[Any] = []
        self.ingest_bodies: List[bytes] = []
        self.pending_commands: Dict[str, List[str]] = {}
        self.polls = 0
        self.uploads: Dict[str, bytes] = {}
        self.completes: Dict[str, Dict[str, Any]] = {}
        self.auth_failures: List[str] = []
        self.fail_next_put = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _body(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _json(self, code: int, obj: Any) -> None:
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self, body: bytes) -> Optional[str]:
                node = self.headers.get("X-Skynet-Node")
                ts = self.headers.get("X-Skynet-Timestamp")
                sig = self.headers.get("X-Skynet-Signature")
                path = self.path.split("?", 1)[0]
                if not (node and ts and sig):
                    server.auth_failures.append(f"missing headers {self.command} {path}")
                    return None
                if abs(int(ts) - time.time_ns()) > 60e9:
                    server.auth_failures.append("stale timestamp")
                    return None
                if not verify_http_request(derive_node_key(server.master, node), self.command, path, int(ts),
                                           body, sig):
                    server.auth_failures.append(f"bad signature {self.command} {path}")
                    return None
                return node

            def do_POST(self) -> None:
                body = self._body()
                parts = self.path.split("?", 1)[0].strip("/").split("/")
                if parts == ["api", "v1", "ingest"]:
                    server.ingest_bodies.append(body)
                    accepted, rejected, nodes = 0, [], set()
                    for i, line in enumerate(body.splitlines()):
                        try:
                            msg = decode_message(line, server.master)
                        except ProtocolError as exc:
                            rejected.append({"line": i, "reason": exc.reason})
                            continue
                        server.ingested.append(msg)
                        nodes.add(msg.node_id)
                        accepted += 1
                    commands = [c for n in nodes for c in server.pending_commands.pop(n, [])]
                    return self._json(200, {"accepted": accepted, "rejected": rejected, "commands": commands})
                if len(parts) == 5 and parts[:3] == ["api", "v1", "clips"] and parts[4] == "upload-target":
                    if self._authed(body) is None:
                        return self._json(401, {"detail": "unauthorized"})
                    rid = parts[3]
                    host = self.headers.get("Host")
                    url = f"/blob/{rid}" if server.put_url_style == "relative" else f"http://{host}/blob/{rid}"
                    return self._json(200, {"method": "PUT", "url": url, "headers": {"Content-Type": "video/mp4"}})
                if len(parts) == 5 and parts[:3] == ["api", "v1", "clips"] and parts[4] == "complete":
                    if self._authed(body) is None:
                        return self._json(401, {"detail": "unauthorized"})
                    server.completes[parts[3]] = json.loads(body)
                    return self._json(200, {"status": "uploaded"})
                return self._json(404, {"detail": "not found"})

            def do_PUT(self) -> None:
                body = self._body()
                parts = self.path.strip("/").split("/")
                if len(parts) == 2 and parts[0] == "blob":
                    if server.fail_next_put > 0:
                        server.fail_next_put -= 1
                        return self._json(503, {"detail": "try again"})
                    if self._authed(body) is None:
                        return self._json(401, {"detail": "unauthorized"})
                    if self.headers.get("Content-Type") != "video/mp4":
                        return self._json(400, {"detail": "content type"})
                    server.uploads[parts[1]] = body
                    return self._json(200, {})
                return self._json(404, {"detail": "not found"})

            def do_GET(self) -> None:
                parts = self.path.split("?", 1)[0].strip("/").split("/")
                if len(parts) == 5 and parts[:3] == ["api", "v1", "nodes"] and parts[4] == "commands":
                    node = self._authed(b"")
                    if node is None or node != parts[3]:
                        return self._json(401, {"detail": "unauthorized"})
                    server.polls += 1
                    return self._json(200, {"commands": server.pending_commands.pop(node, [])})
                return self._json(404, {"detail": "not found"})

        for port in HTTP_PORTS:
            try:
                self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
                break
            except OSError:
                continue
        else:
            raise RuntimeError("no free HTTP port")
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(2)
