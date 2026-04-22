"""Regression gate: baseline loading, golden enforcement, SpreadsheetBench
tolerance window, immutability rule."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import regression
from evals.regression import (
    BASELINE_PATH,
    GoldenFailure,
    SpreadsheetBenchRegression,
    check_golden,
    check_spreadsheetbench,
    enforce,
    evaluate,
    load_baseline,
)
from evals.results.schema import Baseline, CaseResult, SuiteResult


def _ok_case(case_id: str, *, verdict: str = "passed") -> CaseResult:
    return CaseResult(
        case_id=case_id,
        suite="golden",
        verdict=verdict,  # type: ignore[arg-type]
        duration_seconds=0.1,
        langfuse_trace_id="fake-trace",
    )


def _sb_case(case_id: str, *, verdict: str = "passed") -> CaseResult:
    return CaseResult(
        case_id=case_id,
        suite="spreadsheetbench",
        verdict=verdict,  # type: ignore[arg-type]
        duration_seconds=0.1,
        langfuse_trace_id="fake-trace",
    )


# ---------------------------------------------------------------------------
# Baseline file is a real committed artifact and parses.
# ---------------------------------------------------------------------------


def test_committed_baseline_parses() -> None:
    baseline = load_baseline()
    assert isinstance(baseline, Baseline)
    # Committed baseline names the golden cases we ship.
    assert set(baseline.golden.keys()) == {
        "sum_of_column",
        "formula_reference_chain",
        "multi_sheet_total",
    }


def test_committed_baseline_has_no_data_for_spreadsheetbench() -> None:
    """Stage 10 ships the framework only; the SpreadsheetBench baseline
    is zero and moves up only via explicit commit."""
    baseline = load_baseline()
    assert baseline.spreadsheetbench_pass_rate == 0.0


def test_loader_strips_comment_key(tmp_path: Path) -> None:
    custom = tmp_path / "b.json"
    custom.write_text(
        json.dumps(
            {
                "_comment": "human note",
                "golden": {"x": "passed"},
                "spreadsheetbench_pass_rate": 0.5,
                "spreadsheetbench_cases": {},
            }
        )
    )
    baseline = load_baseline(custom)
    assert baseline.golden == {"x": "passed"}


# ---------------------------------------------------------------------------
# Golden enforcement
# ---------------------------------------------------------------------------


def test_golden_all_passing_no_regressions() -> None:
    baseline = Baseline(
        golden={"a": "passed", "b": "passed"},
        spreadsheetbench_pass_rate=0.0,
    )
    current = SuiteResult.from_cases(
        suite="golden", cases=[_ok_case("a"), _ok_case("b")]
    )
    assert check_golden(current, baseline) == []


def test_golden_failed_case_is_regression() -> None:
    baseline = Baseline(golden={"a": "passed", "b": "passed"})
    current = SuiteResult.from_cases(
        suite="golden",
        cases=[_ok_case("a"), _ok_case("b", verdict="failed")],
    )
    regressions = check_golden(current, baseline)
    assert len(regressions) == 1
    assert regressions[0].case_id == "b"


def test_missing_case_in_current_run_counts_as_regression() -> None:
    """If baseline requires case X but the current run didn't execute it,
    that's a regression (we should run every baseline case)."""
    baseline = Baseline(golden={"a": "passed", "b": "passed"})
    current = SuiteResult.from_cases(suite="golden", cases=[_ok_case("a")])
    regressions = check_golden(current, baseline)
    assert [r.case_id for r in regressions] == ["b"]
    assert regressions[0].message == "case missing from current run"


def test_extra_passing_case_not_in_baseline_is_fine() -> None:
    """Running a new case that isn't in the baseline yet is OK — the
    baseline is additive, promotion happens via explicit commit."""
    baseline = Baseline(golden={"a": "passed"})
    current = SuiteResult.from_cases(
        suite="golden", cases=[_ok_case("a"), _ok_case("new_case")]
    )
    assert check_golden(current, baseline) == []


# ---------------------------------------------------------------------------
# SpreadsheetBench tolerance
# ---------------------------------------------------------------------------


def test_bench_within_tolerance_passes() -> None:
    baseline = Baseline(spreadsheetbench_pass_rate=0.60)
    current = SuiteResult.from_cases(
        suite="spreadsheetbench",
        # 59% pass rate — 1% below, within 2% window.
        cases=[
            _sb_case(f"c{i}", verdict="passed" if i < 59 else "failed")
            for i in range(100)
        ],
    )
    passed, delta = check_spreadsheetbench(current, baseline)
    assert passed
    assert delta == pytest.approx(0.01, abs=1e-6)


def test_bench_more_than_two_percent_drop_fails() -> None:
    baseline = Baseline(spreadsheetbench_pass_rate=0.60)
    current = SuiteResult.from_cases(
        suite="spreadsheetbench",
        # 55% pass rate — 5% below baseline.
        cases=[
            _sb_case(f"c{i}", verdict="passed" if i < 55 else "failed")
            for i in range(100)
        ],
    )
    passed, delta = check_spreadsheetbench(current, baseline)
    assert not passed
    assert delta > 0.02


def test_bench_improvement_always_passes() -> None:
    """Asymmetric window: we allow unbounded improvement."""
    baseline = Baseline(spreadsheetbench_pass_rate=0.60)
    current = SuiteResult.from_cases(
        suite="spreadsheetbench",
        cases=[_sb_case(f"c{i}") for i in range(100)],  # 100% pass rate
    )
    passed, delta = check_spreadsheetbench(current, baseline)
    assert passed
    assert delta < 0


def test_empty_bench_run_does_not_fail_gate() -> None:
    """If the bench job didn't run anything (no dataset in CI), the gate
    doesn't block the PR on that alone."""
    baseline = Baseline(spreadsheetbench_pass_rate=0.60)
    current = SuiteResult.from_cases(suite="spreadsheetbench", cases=[])
    passed, _delta = check_spreadsheetbench(current, baseline)
    assert passed


# ---------------------------------------------------------------------------
# evaluate() + enforce()
# ---------------------------------------------------------------------------


def test_evaluate_reports_structured_message() -> None:
    baseline = Baseline(
        golden={"a": "passed"},
        spreadsheetbench_pass_rate=0.60,
    )
    golden = SuiteResult.from_cases(
        suite="golden", cases=[_ok_case("a", verdict="failed")]
    )
    sb = SuiteResult.from_cases(
        suite="spreadsheetbench",
        cases=[_sb_case(f"c{i}", verdict="failed") for i in range(100)],
    )
    report = evaluate(golden=golden, spreadsheetbench=sb, baseline=baseline)
    assert not report.passed
    assert "golden regressions" in report.message
    assert "SpreadsheetBench" in report.message


def test_enforce_raises_golden_first() -> None:
    baseline = Baseline(
        golden={"a": "passed"}, spreadsheetbench_pass_rate=0.60
    )
    golden = SuiteResult.from_cases(
        suite="golden", cases=[_ok_case("a", verdict="failed")]
    )
    report = evaluate(golden=golden, baseline=baseline)
    with pytest.raises(GoldenFailure):
        enforce(report)


def test_enforce_raises_bench_when_no_golden_failure() -> None:
    baseline = Baseline(spreadsheetbench_pass_rate=0.60)
    sb = SuiteResult.from_cases(
        suite="spreadsheetbench",
        cases=[_sb_case(f"c{i}", verdict="failed") for i in range(100)],
    )
    report = evaluate(spreadsheetbench=sb, baseline=baseline)
    with pytest.raises(SpreadsheetBenchRegression):
        enforce(report)


def test_enforce_noop_when_report_passed() -> None:
    baseline = Baseline(golden={}, spreadsheetbench_pass_rate=0.0)
    report = evaluate(baseline=baseline)
    enforce(report)  # no exception


# ---------------------------------------------------------------------------
# Immutability: this module exposes no writer for the baseline file.
# ---------------------------------------------------------------------------


def test_regression_module_exposes_no_baseline_writer() -> None:
    """Stage 10 acceptance: baseline.json is human-edited only."""
    public = {name for name in dir(regression) if not name.startswith("_")}
    # Any name containing "write" / "save" / "update" / "overwrite"
    # would be a regression (pun intended) against the rule.
    forbidden_substrings = ("write", "save", "update_baseline", "overwrite")
    for forbidden in forbidden_substrings:
        for name in public:
            assert forbidden not in name.lower(), (
                f"regression.py exposes {name!r}; baseline must be "
                "human-edited only per docs/decisions/0007-eval-suite.md"
            )


def test_baseline_path_points_to_committed_file() -> None:
    assert BASELINE_PATH.exists()
    assert BASELINE_PATH.name == "baseline.json"
    assert BASELINE_PATH.parent.name == "results"
