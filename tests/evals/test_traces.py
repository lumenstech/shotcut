"""EvalContext tagging + trace id exposure."""
from __future__ import annotations

import pytest

from evals.traces import EvalContext
from shotcut.observability import tracing
from shotcut.observability.tracing import RecordingTracer


@pytest.fixture(autouse=True)
def _recording_tracer() -> RecordingTracer:
    recorder = RecordingTracer()
    tracing.set_tracer(recorder)
    yield recorder
    tracing.reset_tracer()


async def test_context_tags_root_span_for_eval(
    _recording_tracer: RecordingTracer,
) -> None:
    async with EvalContext(suite="golden", case_id="x") as ctx:
        assert ctx.trace_id is not None

    root = next(s for s in _recording_tracer.spans if s.name == "session.turn")
    assert root.attributes["trigger"] == "eval"
    assert root.attributes["eval_suite"] == "golden"
    assert root.attributes["eval_case_id"] == "x"
    assert root.attributes["eval_trace_id"] == str(ctx.trace_id)


async def test_context_propagates_exceptions(
    _recording_tracer: RecordingTracer,
) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with EvalContext(suite="golden", case_id="y"):
            raise RuntimeError("boom")

    root = next(s for s in _recording_tracer.spans if s.name == "session.turn")
    assert isinstance(root.exception, RuntimeError)


async def test_trace_id_is_stable_within_one_context() -> None:
    """Each EvalContext instance has exactly one trace_id; reading it
    before `async with` and after yields the same value — so callers
    can stash it early without awaiting enter."""
    ctx = EvalContext(suite="spreadsheetbench", case_id="z")
    pre = ctx.trace_id
    async with ctx:
        pass
    assert ctx.trace_id == pre


async def test_two_contexts_get_distinct_trace_ids() -> None:
    a = EvalContext(suite="golden", case_id="a")
    b = EvalContext(suite="golden", case_id="b")
    assert a.trace_id != b.trace_id
