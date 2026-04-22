"""Per-case eval runner.

Wraps each case in an `EvalContext` (observability root span tagged
`trigger=eval`) and a stopwatch, invokes a caller-supplied
`producer(prompt) -> Workbook`, scores the actual workbook against
the expected one, and returns a `CaseResult`.

The `producer` indirection is the hook tests use to inject a
deterministic workbook generator — the default producer would run
the full orchestrator and hit Anthropic, which neither CI nor weekly
cron want on every case. Production cron passes a real producer; the
golden-set CI run in this repo ships a producer-less test fixture
that exercises the scoring + reporting surface.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from evals.results.schema import CaseResult, CellDiff
from evals.scoring import score
from evals.traces import EvalContext
from shotcut.spreadsheet.workbook import Workbook


Producer = Callable[[str], Awaitable[Workbook]]


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    prompt: str
    expected: Workbook


async def run_case(
    case: EvalCase,
    *,
    suite: Literal["golden", "spreadsheetbench"],
    producer: Producer,
) -> CaseResult:
    """Score one case. Returns a `CaseResult`; never raises — a
    producer exception is recorded as `verdict=errored`."""
    start = time.monotonic()
    async with EvalContext(suite=suite, case_id=case.case_id) as ctx:
        trace_id = str(ctx.trace_id)
        try:
            actual = await producer(case.prompt)
        except Exception as exc:  # noqa: BLE001
            return CaseResult(
                case_id=case.case_id,
                suite=suite,
                verdict="errored",
                duration_seconds=time.monotonic() - start,
                langfuse_trace_id=trace_id,
                message=f"{type(exc).__name__}: {exc}",
            )

    report = score(case.expected, actual)
    duration = time.monotonic() - start
    if report.passed:
        return CaseResult(
            case_id=case.case_id,
            suite=suite,
            verdict="passed",
            duration_seconds=duration,
            langfuse_trace_id=trace_id,
        )
    return CaseResult(
        case_id=case.case_id,
        suite=suite,
        verdict="failed",
        duration_seconds=duration,
        langfuse_trace_id=trace_id,
        diffs=report.diffs,
        diffs_truncated=report.diffs_truncated,
        message=_summarize(report.diffs, report.diffs_truncated),
    )


def _summarize(diffs: list[CellDiff], truncated: bool) -> str:
    counts: dict[str, int] = {}
    for d in diffs:
        counts[d.axis] = counts.get(d.axis, 0) + 1
    parts = [f"{n} {axis} mismatch{'es' if n != 1 else ''}"
             for axis, n in sorted(counts.items())]
    summary = ", ".join(parts) if parts else "no diffs recorded"
    if truncated:
        summary += " (truncated)"
    return summary


async def run_suite(
    cases: list[EvalCase],
    *,
    suite: Literal["golden", "spreadsheetbench"],
    producer: Producer,
) -> list[CaseResult]:
    """Run every case in `cases` sequentially. Returns the full list
    regardless of individual verdicts — callers decide whether a
    failed case is a gate-stop (golden) or a rate-comparison data
    point (spreadsheetbench)."""
    results: list[CaseResult] = []
    for case in cases:
        results.append(
            await run_case(case, suite=suite, producer=producer)
        )
    return results
