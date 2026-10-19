"""State backends: in-process (single instance) and Redis (shared)."""

from __future__ import annotations

from ..config import Settings
from .base import Admission, AdmitResult, StateBackend


def create_state(settings: Settings) -> StateBackend:
    common = dict(
        events_maxlen=settings.events_maxlen,
        detection_retention_ns=settings.match.retention_ns,
        max_points=settings.tracks.max_points,
        track_retention_ns=settings.tracks.retention_ns,
        replay_window_ns=settings.replay_window_ns,
        node_active_ns=settings.node_active_ns,
    )
    if settings.state_backend == "redis":
        from .redis import RedisState

        assert settings.redis_url
        return RedisState(settings.redis_url, settings.redis_prefix, **common)
    from .memory import MemoryState

    return MemoryState(**common)


__all__ = ["Admission", "AdmitResult", "StateBackend", "create_state"]
