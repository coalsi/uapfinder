"""UDP ingest on the web server's event loop (standalone mode).

The listener owns a non-blocking socket registered with ``loop.add_reader``
and drains it in a loop on every wake-up, instead of using an asyncio
``DatagramProtocol``: the stock selector transport reads exactly one datagram
per event-loop iteration, so the pipeline would always see batches of one.
Draining gives the engine natural batches under load (one round trip per
batch with the Redis backend, less per-packet overhead with the memory one)
while a lone packet is still processed on the next loop iteration. This
works the same on the default loop (kqueue on macOS, epoll on Linux) and on
uvloop.

Clip requests go back out through ``sendto`` on the same socket, so they
leave from the port the node sent to and traverse its NAT mapping.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import socket
import time
from typing import Any, Deque, Optional, Tuple

from .engine import Engine, Inbound

log = logging.getLogger("skynet.udp")

_RECV_SIZE = 2048  # larger than any valid message (1200 bytes); longer ones are rejected as too_large
_MIN_RCVBUF = 256 * 1024


class UdpListener:
    def __init__(self, engine: Engine, host: str, port: int, rcvbuf: int = 8 * 1024 * 1024, batch_max: int = 64,
                 queue_max: int = 200_000, reuse_port: bool = False):
        self.engine = engine
        self.host = host
        self.port = port
        self.rcvbuf = rcvbuf
        self.batch_max = batch_max
        self.queue_max = queue_max
        self.reuse_port = reuse_port
        self.sock: Optional[socket.socket] = None
        self._queue: Deque[Inbound] = collections.deque()
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._pending: Deque[Tuple[bytes, Any]] = collections.deque()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self) -> None:
        loop = self._loop = asyncio.get_running_loop()
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.setblocking(False)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)  # "::" also accepts IPv4
        if self.reuse_port:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self._set_rcvbuf(sock)
        sock.bind((self.host, self.port))
        self.port = sock.getsockname()[1]
        self.sock = sock
        loop.add_reader(sock.fileno(), self._readable)
        self._task = loop.create_task(self._consume(), name="skynet-udp")
        self.engine.transport = self
        log.info("UDP ingest listening on %s:%d", self.host, self.port)

    def _set_rcvbuf(self, sock: socket.socket) -> None:
        # Linux silently caps the size at net.core.rmem_max; macOS refuses
        # values above kern.ipc.maxsockbuf, so step down until one is accepted.
        size = self.rcvbuf
        while size >= _MIN_RCVBUF:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, size)
                return
            except OSError:
                size //= 2

    async def stop(self) -> None:
        if self.sock is not None and self._loop is not None:
            self._loop.remove_reader(self.sock.fileno())
            self._loop.remove_writer(self.sock.fileno())
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.engine.transport is self:
            self.engine.transport = None

    def _readable(self) -> None:
        sock = self.sock
        assert sock is not None
        queue = self._queue
        stats = self.engine.stats
        # Bounded so a flood cannot starve the HTTP side of the event loop.
        for _ in range(4 * self.batch_max):
            try:
                data, addr = sock.recvfrom(_RECV_SIZE)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                # e.g. ECONNREFUSED: an ICMP error for an earlier sendto to a node that went away.
                stats.count("udp_recv_errors")
                continue
            if len(queue) >= self.queue_max:
                stats.count("udp_dropped_overload")
                continue
            queue.append(Inbound(data, time.time_ns(), time.perf_counter_ns(), addr))
        if queue:
            self._wake.set()

    async def _consume(self) -> None:
        queue = self._queue
        while True:
            await self._wake.wait()
            self._wake.clear()
            while queue:
                n = min(len(queue), self.batch_max)
                batch = [queue.popleft() for _ in range(n)]
                try:
                    await self.engine.process(batch)
                except Exception:  # noqa: BLE001 - one bad batch (e.g. Redis down) must not stop ingest
                    self.engine.stats.count("udp_batch_errors")
                    log.exception("UDP batch of %d messages failed", n)
                # Let the reader and HTTP handlers run between batches.
                await asyncio.sleep(0)

    # --- back-signal ---------------------------------------------------------------

    def sendto(self, data: bytes, addr: Any) -> None:
        sock = self.sock
        if sock is None:
            return
        if self._pending:
            self._pending.append((data, addr))
            return
        try:
            sock.sendto(data, addr)
        except (BlockingIOError, InterruptedError):
            self._pending.append((data, addr))
            assert self._loop is not None
            self._loop.add_writer(sock.fileno(), self._writable)
        except OSError:
            self.engine.stats.count("udp_send_errors")

    def _writable(self) -> None:
        sock = self.sock
        assert sock is not None and self._loop is not None
        while self._pending:
            data, addr = self._pending[0]
            try:
                sock.sendto(data, addr)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self.engine.stats.count("udp_send_errors")
            self._pending.popleft()
        self._loop.remove_writer(sock.fileno())
