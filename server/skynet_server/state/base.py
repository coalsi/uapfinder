"""The state backend interface.

The ingest pipeline is batch-oriented so that a Redis-backed deployment pays
a handful of round trips per *batch* (one UDP drain or one NDJSON request)
rather than per packet. Each hot-path method below corresponds to one
pipelined round trip in ``RedisState``:

1. ``admit``            replay check, node upsert, detection store and then
                         the neighbour search, in that order (store first,
                         then query: of two simultaneous packets processed by
                         different instances, at least one sees the other);
2. ``fetch_detections`` neighbours' recent detections (+ active filter);
3. ``claim``            pair de-duplication (SET NX with TTL);
4. ``get_links`` / ``get_tracks``  local track -> 3D track resolution;
5. ``commit``           track state, points, events, clips, commands, stats.

``MemoryState`` implements the same contract in-process for single-instance
deployments; the shared test-suite runs against both.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..models import ClipRecord, Commit, Detection, NodeRecord, NodeUpdate, TrackState
from ..stats import StatsDelta

UdpAddr = Tuple[str, int]


@dataclass(slots=True)
class Admission:
    update: NodeUpdate
    seq: int
    detection: Optional[Detection] = None


@dataclass
class AdmitResult:
    fresh: List[bool]
    neighbours: Dict[str, List[str]] = field(default_factory=dict)
    nodes: Dict[str, NodeRecord] = field(default_factory=dict)  # records refreshed by heartbeats


def event_id_key(event_id: str) -> Tuple[int, int]:
    """Sort key of a stream id ``"<ms>-<seq>"``; malformed ids sort first."""
    ms, _, seq = event_id.partition("-")
    try:
        return int(ms), int(seq or 0)
    except ValueError:
        return (-1, -1)


class StateBackend(ABC):
    name = "abstract"

    async def start(self) -> None:
        """Open connections; called once from the application lifespan."""

    async def close(self) -> None:
        """Release connections."""

    async def ping(self) -> bool:
        return True

    # --- ingest hot path -------------------------------------------------

    @abstractmethod
    async def admit(self, items: Sequence[Admission], now_ns: int, radius_m: float,
                    udp_addrs: Optional[Dict[str, UdpAddr]] = None) -> AdmitResult:
        """Drop replays, apply node updates and store detections, then find
        the neighbours (within ``radius_m``, excluding the node itself) of
        every node that reported a detection, and return the updated records
        of nodes that sent a heartbeat. ``udp_addrs`` are source addresses
        that changed and should be shared with other workers."""

    @abstractmethod
    async def fetch_detections(self, windows: Dict[str, Tuple[int, int]], active_since_ns: int
                               ) -> Dict[str, List[Detection]]:
        """Detections of each node inside its [lo_ns, hi_ns] window, oldest
        first, for nodes seen since ``active_since_ns``."""

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

    # --- commands and clips ----------------------------------------------

    @abstractmethod
    async def pop_commands(self, node_ids: Sequence[str]) -> Dict[str, List[str]]:
        """Remove and return pending signed commands (each returned once)."""

    @abstractmethod
    async def get_clip(self, request_id: str) -> Optional[ClipRecord]: ...

    @abstractmethod
    async def put_clip(self, clip: ClipRecord) -> None: ...

    async def append_events(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return await self.commit(Commit(events=events), link_ttl_ms=0)

    # --- shared statistics ------------------------------------------------

    async def flush_stats(self, delta: StatsDelta) -> None:
        """Publish a process's stats deltas (shared backends only)."""

    async def shared_stats(self) -> Optional[Dict[str, Any]]:
        """Aggregated stats across processes, or None if stats are process-local."""
        return None
