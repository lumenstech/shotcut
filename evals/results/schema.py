"""Pydantic models for eval results and the baseline file.

The baseline stores per-case verdict, not just an aggregate pass
rate, so SpreadsheetBench regressions are traceable to specific cases
and golden-set 100% failures point at a named model.

The file is committed at `evals/results/baseline.json` and human-
edited only — `regression.py` reads it, never writes it.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


Verdict = Literal["passed", "failed", "errored"]


class CellDiff(BaseModel):
    """One difference between expected and actual workbook state."""

    axis: Literal["cell_value", "formula", "structure"]
    location: str  # "Sheet!A1", "sheet_name", etc.
    expected: Any = None
    actual: Any = None
    note: str | None = None  # e.g. "formula_drift: same evaluated value"


class CaseResult(BaseModel):
    """One row per eval case."""

    case_id: str
    suite: Literal["golden", "spreadsheetbench"]
    verdict: Verdict
    duration_seconds: float
    langfuse_trace_id: str
    diffs: list[CellDiff] = []
    diffs_truncated: bool = False
    message: str | None = None


class SuiteResult(BaseModel):
    """Full run of one suite, aggregating CaseResult rows."""

    suite: Literal["golden", "spreadsheetbench"]
    total: int
    passed: int
    failed: int
    errored: int
    cases: list[CaseResult]

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @classmethod
    def from_cases(
        cls, *, suite: Literal["golden", "spreadsheetbench"], cases: list[CaseResult]
    ) -> "SuiteResult":
        total = len(cases)
        passed = sum(1 for c in cases if c.verdict == "passed")
        failed = sum(1 for c in cases if c.verdict == "failed")
        errored = sum(1 for c in cases if c.verdict == "errored")
        return cls(
            suite=suite,
            total=total,
            passed=passed,
            failed=failed,
            errored=errored,
            cases=cases,
        )


class Baseline(BaseModel):
    """The committed, human-edited reference. `regression.py` compares
    a current SuiteResult against this and fails the PR on regression."""

    golden: dict[str, Verdict] = {}
    spreadsheetbench_pass_rate: float = 0.0
    spreadsheetbench_cases: dict[str, Verdict] = {}
