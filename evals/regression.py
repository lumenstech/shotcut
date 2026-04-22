"""CI regression gate.

Two rules, both read-only against `evals/results/baseline.json`:

1. **Golden set**: every case named in `baseline["golden"]` must have
   verdict `"passed"` in the current suite result. A single failure
   returns `GoldenFailure`. Extra cases in the current run that aren't
   in the baseline are ignored (new cases are OK until committed into
   baseline).
2. **SpreadsheetBench**: current pass rate must be at least
   `baseline["spreadsheetbench_pass_rate"] - 0.02`. More than 2% drop
   returns `SpreadsheetBenchRegression`.

The baseline file is read-only from this module. The decision doc
explicitly forbids auto-update; ratchets happen through a reviewed
commit with a written reason. Any helper that would write back to the
file belongs outside `regression.py`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from evals.results.schema import Baseline, CaseResult, SuiteResult

BASELINE_PATH = Path(__file__).resolve().parent / "results" / "baseline.json"


class GateFailure(Exception):
    """Base type for CI-gate failures. CI catches this and exits
    non-zero with the message."""


class GoldenFailure(GateFailure):
    """A case required to pass by the baseline didn't."""


class SpreadsheetBenchRegression(GateFailure):
    """Current pass rate dropped more than 2% below baseline."""


@dataclass(frozen=True)
class GateReport:
    """Machine-readable summary. CI surfaces this to humans."""

    golden_failures: list[CaseResult]
    spreadsheetbench_current_rate: float
    spreadsheetbench_baseline_rate: float
    passed: bool
    message: str


def load_baseline(path: Path | None = None) -> Baseline:
    """Read the committed baseline. `path` override is for tests only."""
    target = path or BASELINE_PATH
    raw = json.loads(target.read_text())
    # Drop the underscore-prefixed `_comment` key before Pydantic parse.
    stripped = {k: v for k, v in raw.items() if not k.startswith("_")}
    return Baseline.model_validate(stripped)


def check_golden(
    current: SuiteResult, baseline: Baseline
) -> list[CaseResult]:
    """Return the list of golden-set cases that regressed.

    Regression = case named `passed` in the baseline but has verdict
    != `passed` in the current run. Missing-from-current counts as
    regression (we're meant to run every baseline case).
    """
    current_by_id = {c.case_id: c for c in current.cases}
    regressed: list[CaseResult] = []
    for case_id, expected in baseline.golden.items():
        if expected != "passed":
            # Non-"passed" baseline verdicts (if the file ever stores
            # them) are advisory — no gate.
            continue
        actual = current_by_id.get(case_id)
        if actual is None or actual.verdict != "passed":
            regressed.append(
                actual
                or CaseResult(
                    case_id=case_id,
                    suite="golden",
                    verdict="errored",
                    duration_seconds=0.0,
                    langfuse_trace_id="(not-run)",
                    message="case missing from current run",
                )
            )
    return regressed


def check_spreadsheetbench(
    current: SuiteResult, baseline: Baseline, *, tolerance: float = 0.02
) -> tuple[bool, float]:
    """Return (passed, delta_below_baseline). `passed` is False if the
    current rate is more than `tolerance` below baseline."""
    if current.total == 0:
        # Nothing ran; don't regress the gate on an empty run.
        return True, 0.0
    delta = baseline.spreadsheetbench_pass_rate - current.pass_rate
    return delta <= tolerance, delta


def evaluate(
    *,
    golden: SuiteResult | None = None,
    spreadsheetbench: SuiteResult | None = None,
    baseline: Baseline | None = None,
    baseline_path: Path | None = None,
) -> GateReport:
    """Run both gates against the current suite results; return a
    structured report. Does not raise — callers inspect `passed` and
    choose their exit code.
    """
    if baseline is None:
        baseline = load_baseline(baseline_path)

    golden_failures: list[CaseResult] = []
    if golden is not None:
        golden_failures = check_golden(golden, baseline)

    sb_passed = True
    sb_current_rate = 0.0
    sb_baseline_rate = baseline.spreadsheetbench_pass_rate
    if spreadsheetbench is not None:
        sb_current_rate = spreadsheetbench.pass_rate
        sb_passed, _delta = check_spreadsheetbench(spreadsheetbench, baseline)

    passed = not golden_failures and sb_passed
    messages: list[str] = []
    if golden_failures:
        names = ", ".join(c.case_id for c in golden_failures[:5])
        more = (
            f" and {len(golden_failures) - 5} more"
            if len(golden_failures) > 5
            else ""
        )
        messages.append(f"golden regressions: {names}{more}")
    if not sb_passed:
        messages.append(
            "SpreadsheetBench pass rate "
            f"{sb_current_rate:.3f} is >2% below baseline {sb_baseline_rate:.3f}"
        )
    message = "; ".join(messages) if messages else "all gates passed"

    return GateReport(
        golden_failures=golden_failures,
        spreadsheetbench_current_rate=sb_current_rate,
        spreadsheetbench_baseline_rate=sb_baseline_rate,
        passed=passed,
        message=message,
    )


def enforce(report: GateReport) -> None:
    """Raise the first applicable `GateFailure` so CI exits non-zero."""
    if report.passed:
        return
    if report.golden_failures:
        raise GoldenFailure(report.message)
    raise SpreadsheetBenchRegression(report.message)
