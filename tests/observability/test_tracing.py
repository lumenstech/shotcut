"""Tracing primitives: context inheritance + backend pluggability."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from shotcut.observability.tracing import (
    NoOpSpan,
    NoOpTracer,
    RecordedSpan,
    RecordingTracer,
    current_span,
    reset_tracer,
    set_tracer,
    start_span,
)


@pytest.fixture(autouse=True)
def _reset_tracer():
    """Install a fresh RecordingTracer for every test and restore after."""
    recorder = RecordingTracer()
    set_tracer(recorder)
    yield recorder
    reset_tracer()


def test_noop_tracer_is_zero_cost() -> None:
    """NoOpTracer returns NoOpSpans that swallow every operation."""
    set_tracer(NoOpTracer())
    with start_span("whatever", key="value") as span:
        assert isinstance(span, NoOpSpan)
        span.set_attribute("other", 42)
    # No exception, no state leak.


def test_recorded_span_captures_attributes(_reset_tracer: RecordingTracer) -> None:
    with start_span("session.turn", session_id="abc", tenant_id="t1") as span:
        assert isinstance(span, RecordedSpan)
        span.set_attribute("outcome", "ok")

    assert len(_reset_tracer.spans) == 1
    recorded = _reset_tracer.spans[0]
    assert recorded.name == "session.turn"
    assert recorded.attributes["session_id"] == "abc"
    assert recorded.attributes["tenant_id"] == "t1"
    assert recorded.attributes["outcome"] == "ok"
    assert recorded.ended_at is not None


def test_nested_spans_inherit_parent(_reset_tracer: RecordingTracer) -> None:
    """contextvar-based inheritance: child.parent_id == outer.id without
    callers passing handles."""
    with start_span("outer") as outer:
        with start_span("middle") as middle:
            with start_span("inner") as inner:
                pass

    assert isinstance(outer, RecordedSpan)
    assert isinstance(middle, RecordedSpan)
    assert isinstance(inner, RecordedSpan)
    assert outer.parent_id is None
    assert middle.parent_id == outer.id
    assert inner.parent_id == middle.id


def test_span_exit_pops_contextvar(_reset_tracer: RecordingTracer) -> None:
    """After `with` exits, current_span() returns to the previous frame's
    span — even if an exception is raised."""
    assert current_span() is None
    with start_span("outer"):
        assert current_span() is not None
        with start_span("inner"):
            pass
        assert current_span() is not None  # back to outer
    assert current_span() is None


def test_exception_recorded_on_span_and_reraised(
    _reset_tracer: RecordingTracer,
) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with start_span("faulty"):
            raise RuntimeError("boom")

    assert len(_reset_tracer.spans) == 1
    assert isinstance(_reset_tracer.spans[0].exception, RuntimeError)


def test_contextvar_propagates_through_await(
    _reset_tracer: RecordingTracer,
) -> None:
    """Async: child spans inside awaited coroutines see the outer parent."""
    async def inner() -> None:
        with start_span("child"):
            await asyncio.sleep(0)

    async def driver() -> None:
        with start_span("parent"):
            await inner()

    # `asyncio.run` (not `get_event_loop().run_until_complete`) — the
    # latter is deprecated on 3.10+ and fails intermittently after
    # other tests close the main-thread loop.
    asyncio.run(driver())
    parent = next(s for s in _reset_tracer.spans if s.name == "parent")
    child = next(s for s in _reset_tracer.spans if s.name == "child")
    assert child.parent_id == parent.id


def test_concurrent_coroutines_get_isolated_span_trees(
    _reset_tracer: RecordingTracer,
) -> None:
    """Two coroutines running concurrently each see their own root span,
    even though they share the event loop. contextvars are per-task."""
    async def worker(name: str) -> None:
        with start_span(f"root.{name}"):
            with start_span(f"child.{name}"):
                await asyncio.sleep(0)

    async def driver() -> None:
        await asyncio.gather(worker("a"), worker("b"))

    asyncio.run(driver())
    roots = {s.name: s for s in _reset_tracer.spans if s.name.startswith("root.")}
    children = {s.name: s for s in _reset_tracer.spans if s.name.startswith("child.")}
    assert children["child.a"].parent_id == roots["root.a"].id
    assert children["child.b"].parent_id == roots["root.b"].id


def test_recording_tracer_clear(_reset_tracer: RecordingTracer) -> None:
    with start_span("one"):
        pass
    assert len(_reset_tracer.spans) == 1
    _reset_tracer.clear()
    assert _reset_tracer.spans == []


# Marker to keep linters from flagging `uuid` as unused — the module
# is imported for the downstream Span.id type but tests don't need to
# construct uuids themselves.
_ = uuid
