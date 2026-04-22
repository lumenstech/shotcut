"""Per-case runner: verdict paths + trace attribution.

Exercises three outcomes — passed, failed, errored — and validates the
Stage 10 amendment that every CaseResult carries a langfuse_trace_id
so failure reports are clickable into the observability stack.
"""
from __future__ import annotations

import pytest
from openpyxl import Workbook as OpenpyxlWorkbook

from evals.results.schema import CaseResult
from evals.runner import EvalCase, run_case, run_suite
from shotcut.observability import tracing
from shotcut.observability.tracing import RecordingTracer
from shotcut.spreadsheet.workbook import Workbook


@pytest.fixture(autouse=True)
def _recording_tracer() -> RecordingTracer:
    recorder = RecordingTracer()
    tracing.set_tracer(recorder)
    yield recorder
    tracing.reset_tracer()


def _wb_with(a1_value: object) -> Workbook:
    pyxl = OpenpyxlWorkbook()
    pyxl.active.title = "S"
    pyxl.active["A1"] = a1_value
    return Workbook(pyxl)


async def _identity_producer(expected: Workbook):
    async def produce(_prompt: str) -> Workbook:
        return expected
    return produce


async def _wrong_producer(_prompt: str) -> Workbook:
    return _wb_with(9999)


async def _erroring_producer(_prompt: str) -> Workbook:
    raise RuntimeError("producer blew up")


async def test_passed_case_returns_verdict_and_trace_id(
    _recording_tracer: RecordingTracer,
) -> None:
    expected = _wb_with(42)
    case = EvalCase(case_id="match", prompt="p", expected=expected)
    producer = await _identity_producer(expected)

    result = await run_case(case, suite="golden", producer=producer)

    assert result.verdict == "passed"
    assert result.case_id == "match"
    assert result.langfuse_trace_id
    assert result.diffs == []
    # Trace was opened with the eval taxonomy attributes.
    root = next(s for s in _recording_tracer.spans if s.name == "session.turn")
    assert root.attributes["trigger"] == "eval"
    assert root.attributes["eval_suite"] == "golden"
    assert root.attributes["eval_case_id"] == "match"


async def test_failed_case_carries_diffs_and_message(
    _recording_tracer: RecordingTracer,
) -> None:
    case = EvalCase(case_id="mismatch", prompt="p", expected=_wb_with(42))

    result = await run_case(case, suite="golden", producer=_wrong_producer)

    assert result.verdict == "failed"
    assert len(result.diffs) >= 1
    assert result.message is not None
    assert "mismatch" in result.message
    assert result.langfuse_trace_id  # still populated so report links through


async def test_producer_exception_becomes_errored_verdict(
    _recording_tracer: RecordingTracer,
) -> None:
    case = EvalCase(case_id="boom", prompt="p", expected=_wb_with(1))

    result = await run_case(case, suite="golden", producer=_erroring_producer)

    assert result.verdict == "errored"
    assert result.message and "RuntimeError" in result.message
    assert "blew up" in result.message


async def test_eval_context_distinguishes_suites(
    _recording_tracer: RecordingTracer,
) -> None:
    """A SpreadsheetBench case's root span carries `eval_suite="spreadsheetbench"`
    so production dashboards filter cleanly."""
    case = EvalCase(case_id="sb-1", prompt="p", expected=_wb_with(1))
    producer = await _identity_producer(case.expected)
    await run_case(case, suite="spreadsheetbench", producer=producer)

    root = next(s for s in _recording_tracer.spans if s.name == "session.turn")
    assert root.attributes["eval_suite"] == "spreadsheetbench"


async def test_run_suite_returns_one_result_per_case(
    _recording_tracer: RecordingTracer,
) -> None:
    cases = [
        EvalCase(case_id=f"c{i}", prompt=f"p{i}", expected=_wb_with(i))
        for i in range(5)
    ]
    producers = {c.prompt: c.expected for c in cases}

    async def producer(prompt: str) -> Workbook:
        return producers[prompt]

    results: list[CaseResult] = await run_suite(
        cases, suite="golden", producer=producer
    )
    assert len(results) == 5
    assert all(r.verdict == "passed" for r in results)
    assert {r.case_id for r in results} == {c.case_id for c in cases}
