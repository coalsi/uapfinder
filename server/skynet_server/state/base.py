"""The state backend interface.

The ingest pipeline is batch-oriented so that a Redis-backed deployment pays
a handful of round trips per *batch* (one UDP drain or one NDJSON request)
rather than per packet. The hot-path methods map onto pipelined round trips
in ``RedisState``:

1. ``neighbours``  active nodes within ``d_max`` of each reporter (cached
                   per process; Redis only goes to the server on a miss);
2. ``admit``       replay check, node upsert and detection store, followed in
                   the same pipeline by the neighbour detection fetch (store
                   first, then query: of two simultaneous packets handled by
                   different instances, at least one sees the other);
3. ``fetch_more``  only when a guided search fell back to a full one;
4. ``claim`` + ``get_links``, then ``get_tracks``  pair de-duplication and
                   local track -> 3D track resolution;
5. ``commit``      track state, points, events, clips, commands, stats.

``MemoryState`` implements the same contract in-process for single-instance
deployments; the shared test-suite runs against both.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..matchmaker import DetectionSource, Window
from ..models import ClipRecord, Commit, Detection, NodeRecord, NodeUpdate, TrackState
from ..stats import StatsDelta

UdpAddr = Tuple[str, int]

EVENT_ID_RE = re.compile(r"^\d{1,20}-\d{1,20}$")


@dataclass(slots=True)
class Admission:
    update: NodeUpdate
    seq: int
    detection: Optional[Detection] = None


@dataclass
class AdmitResult:
    fresh: List[bool]
    source: DetectionSource
    nodes: Dict[str, NodeRecord] = field(default_factory=dict)  # records refreshed by heartbeats


def event_id_key(event_id: str) -> Tuple[int, int]:
    """Sort key of a stream id ``"<ms>-<seq>"`` (validated by the API)."""
    ms, _, seq = event_id.partition("-")
    return int(ms), int(seq)


class StateBackend(ABC):
    name = "abstract"
    #: True when neighbour detections must be fetched per batch (``admit``
    #: windows / ``fetch_more``); False when the backend reads them in place.
    prefetch = False

    async def start(self) -> None:
        """Open connections; called once from the application lifespan."""

    async def close(self) -> None:
        """Release connections."""

    async def ping(self) -> bool:
        return True

    # --- ingest hot path -------------------------------------------------

    @abstractmethod
    async def neighbours(self, reporters: Dict[str, Tuple[float, float]], radius_m: float, now_ns: int
                         ) -> Dict[str, List[str]]:
        """Active nodes within ``radius_m`` of each reporter, excluding itself."""

    @abstractmethod
    async def admit(self, items: Sequence[Admission], now_ns: int, windows: Optional[Dict[str, Window]] = None
                    ) -> AdmitResult:
        """Drop replays (same node, seq and timestamp), apply node updates and
        store detections, then fetch the neighbour detections in ``windows``.
        Returns per-item freshness, a source for neighbour detections and the
        refreshed records of nodes that sent a heartbeat."""

    async def fetch_more(self, source: DetectionSource, windows: Dict[str, Window]) -> None:
        """Extend ``source`` with more windows (prefetching backends only)."""

    @abstractmethod
    async def claim(self, keys: Sequence[str], ttl_ms: int) -> List[bool]:
        """Atomically claim each key (True if it was not claimed before)."""

    @abstractmethod
    async def get_links(self, keys: Sequence[str]) -> Dict[str, str]:
        """Map of ``"<node_id>|<local track id>"`` -> 3D track id for known links."""

    @abstractmethod
    async def get_tracks(self, track_ids: Sequence[str]) -> Dict[str, TrackState]:
        """Live track states by id (missing ids are omitted)."""

    @abstractmethod
    async def commit(self, commit: Commit, link_ttl_ms: int, stats: Optional[StatsDelta] = None
                     ) -> List[Dict[str, Any]]:
        """Write a batch's results; returns the events with their ids assigned."""

    @abstractmethod
    async def get_udp_addrs(self, node_ids: Sequence[str]) -> Dict[str, Tuple[UdpAddr, int]]:
        """Shared UDP return addresses: node_id -> ((host, port), last_seen_ns)."""

    # --- nodes -----------------------------------------------------------

    @abstractmethod
    async def register_node(self, node_id: str, label: Optional[str], now_ns: int) -> Tuple[NodeRecord, bool]:
        """Create (or relabel) a node; returns (record, created)."""

    @abstractmethod
    async def list_nodes(self) -> List[NodeRecord]: ...

    @abstractmethod
    async def get_node(self, node_id: str) -> Optional[NodeRecord]: ...

    @abstractmethod
    async def count_active_nodes(self, since_ns: int) -> int: ...

    # --- tracks ----------------------------------------------------------

    @abstractmethod
    async def list_tracks(self, since_ns: int, limit: int, classification: Optional[str]) -> List[TrackState]:
        """Tracks with ``last_ns >= since_ns``, newest first."""

    @abstractmethod
    async def get_track_points(self, track_ids: Sequence[str], limit: int) -> Dict[str, List[Dict[str, Any]]]:
        """The most recent ``limit`` points of each track, oldest first."""

    @abstractmethod
    async def count_active_tracks(self, since_ns: int) -> int: ...

    # --- events ----------------------------------------------------------

    @abstractmethod
    async def events_after(self, cursor: Optional[str], limit: int) -> List[Dict[str, Any]]:
        """Events strictly after ``cursor`` (oldest first), or the most
        recent ``limit`` events when ``cursor`` is None."""

    @abstractmethod
    async def last_event_id(self) -> Optional[str]: ...

    async def append_events(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return await self.commit(Commit(events=events), link_ttl_ms=0)

    # --- commands and clips ----------------------------------------------

    @abstractmethod
    async def pop_commands(self, node_ids: Sequence[str]) -> Dict[str, List[str]]:
        """Remove and return pending signed commands (each returned once)."""

    @abstractmethod
    async def get_clip(self, request_id: str) -> Optional[ClipRecord]: ...

    @abstractmethod
    async def put_clip(self, clip: ClipRecord) -> None: ...

    @abstractmethod
    async def track_clips(self, track_id: str) -> List[ClipRecord]: ...

    # --- misc --------------------------------------------------------------

    @abstractmethod
    async def rate_limit(self, key: str, limit: int, window_s: float) -> bool:
        """Record one request for ``key``; False if more than ``limit``
        requests (including this one) fell inside the sliding window."""

    async def shared_stats(self) -> Optional[Dict[str, Any]]:
        """Aggregated stats across processes, or None if stats are process-local."""
        return None
