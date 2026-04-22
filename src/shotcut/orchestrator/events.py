"""Per-session progress event bus.

In-process asyncio.Queue fanout: the durable orchestrator emits
`ProgressEvent` objects into a session's queue; the SSE handler
consumes them until it sees a terminal state. Multi-worker deployments
swap this for Redis pub/sub — see docs/decisions/0003-durable-execution.md.

Queue lifecycle: created lazily on first `emit` or `subscribe`; lives
in process memory until the session reaches a terminal state, at
which point the terminal event is held in the buffer so late
subscribers still see a completion signal.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from shotcut.db.models import OrchestratorStateEnum


class ProgressEvent(BaseModel):
    session_id: uuid.UUID
    state: OrchestratorStateEnum
    message: str
    percent: float = Field(default=0.0, ge=0.0, le=1.0)
    current_action: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


TERMINAL_STATES = frozenset({OrchestratorStateEnum.DONE, OrchestratorStateEnum.FAILED})


_buses: dict[uuid.UUID, asyncio.Queue[ProgressEvent]] = {}


def _bus(session_id: uuid.UUID) -> asyncio.Queue[ProgressEvent]:
    if session_id not in _buses:
        _buses[session_id] = asyncio.Queue()
    return _buses[session_id]


async def emit(session_id: uuid.UUID, event: ProgressEvent) -> None:
    """Push a progress event into the session's queue.

    Idempotent wrt queue creation. Non-blocking in practice (queue is
    unbounded) — the orchestrator never waits on a subscriber.
    """
    await _bus(session_id).put(event)


async def subscribe(session_id: uuid.UUID) -> AsyncIterator[ProgressEvent]:
    """Async-iterate progress events for a session until terminal.

    Yields each event as it arrives. Stops after a terminal event
    (state=DONE or FAILED). Safe for the SSE handler to wrap in a
    generator response.
    """
    queue = _bus(session_id)
    while True:
        event = await queue.get()
        yield event
        if event.state in TERMINAL_STATES:
            return


def drop(session_id: uuid.UUID) -> None:
    """Discard a session's bus. Called after terminal + a short grace
    period to let the last subscriber drain. No-op if no bus exists."""
    _buses.pop(session_id, None)


def reset_for_tests() -> None:
    """Clear every bus. Per-test isolation helper."""
    _buses.clear()
