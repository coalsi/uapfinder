"""State backends: in-process (single instance) and Redis (shared)."""

from __future__ import annotations

from ..config import Settings
from .base import Admission, AdmitResult, StateBackend


def create_state(settings: Settings) -> StateBackend:
    if settings.state_backend == "redis":
        from .redis import RedisState

        assert settings.redis_url
        return RedisState(settings.redis_url, settings.redis_prefix, settings.events_maxlen)
    from .memory import MemoryState

    return MemoryState(settings.events_maxlen)


__all__ = ["Admission", "AdmitResult", "StateBackend", "create_state"]
