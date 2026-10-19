"""Live event fan-out for ``WS /api/v1/live``.

Single-process deployments (memory backend) broadcast in-process: the
pipeline publishes each committed batch of events to every subscriber's
bounded queue. A subscriber that falls behind loses its oldest events rather
than slowing ingest; it can backfill through ``GET /events?after=``.

With the Redis backend the events of all workers/instances are in the Redis
stream, so each WebSocket handler forwards from the stream instead
(``stream_events``). On Vercel that is the only option, since every Function
instance sees only the requests it serves. Either way the starting point is
fixed before the socket is accepted, so a client misses nothing published
after it connected.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from .state.base import StateBackend

_QUEUE_LIMIT = 2_000


class Subscription:
    """Events published after the subscription was created, in order."""

    def __init__(self, hub: "EventHub") -> None:
        self._hub = hub
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        hub._subscribers.add(self.queue)  # registered now, not on first iteration

    def close(self) -> None:
        self._hub._subscribers.discard(self.queue)

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> Dict[str, Any]:
        return await self.queue.get()


class EventHub:
    def __init__(self) -> None:
        self._subscribers: Set[asyncio.Queue] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, events: List[Dict[str, Any]]) -> None:
        if not self._subscribers:
            return
        for q in self._subscribers:
            for event in events:
                if q.full():
                    q.get_nowait()
                q.put_nowait(event)

    def subscribe(self) -> Subscription:
        return Subscription(self)


async def stream_events(state: StateBackend, poll_s: float, cursor: str) -> AsyncIterator[Dict[str, Any]]:
    """Follow the shared event stream from just after ``cursor``."""
    while True:
        batch = await state.events_after(cursor, 500)
        if batch:
            cursor = batch[-1]["id"]
            for event in batch:
                yield event
            if len(batch) == 500:
                continue
        await asyncio.sleep(poll_s)
