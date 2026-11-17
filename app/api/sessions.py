"""Conversation history storage.

``SessionStore`` is the interface the API depends on; ``InMemorySessionStore``
is the v1 implementation (single process only). A Redis implementation can
replace it later without touching the API code.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Turn:
    """One exchange: the visitor message and the answer the visitor saw."""

    user: str
    assistant: str


class SessionStore(Protocol):
    """Storage for per-session conversation history."""

    async def get_turns(self, session_id: str) -> list[Turn]:
        """Return the stored turns, oldest first (empty if unknown/expired)."""
        ...

    async def append_turn(self, session_id: str, turn: Turn) -> None:
        """Store a turn, keeping only the most recent turns."""
        ...

    async def clear(self, session_id: str) -> bool:
        """Delete a session's history; return True if it existed."""
        ...


@dataclass
class _Session:
    turns: deque[Turn]
    last_seen: float = field(default=0.0)


class InMemorySessionStore:
    """Process-local store: last ``max_turns`` turns, idle expiry after TTL."""

    def __init__(self, max_turns: int, ttl_s: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._max_turns = max_turns
        self._ttl_s = ttl_s
        self._clock = clock
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()

    def _purge_expired(self, now: float) -> None:
        expired = [sid for sid, s in self._sessions.items()
                   if now - s.last_seen > self._ttl_s]
        for sid in expired:
            del self._sessions[sid]

    async def get_turns(self, session_id: str) -> list[Turn]:
        """Return the stored turns, oldest first (empty if unknown/expired)."""
        async with self._lock:
            self._purge_expired(self._clock())
            session = self._sessions.get(session_id)
            return list(session.turns) if session else []

    async def append_turn(self, session_id: str, turn: Turn) -> None:
        """Store a turn, keeping only the most recent turns."""
        async with self._lock:
            now = self._clock()
            self._purge_expired(now)
            session = self._sessions.setdefault(
                session_id, _Session(deque(maxlen=self._max_turns)))
            session.turns.append(turn)
            session.last_seen = now

    async def clear(self, session_id: str) -> bool:
        """Delete a session's history; return True if it existed."""
        async with self._lock:
            return self._sessions.pop(session_id, None) is not None
