"""Workbook comparison: cell values, formulas (syntactic + evaluated),
structure.

Produces a list of `CellDiff` rows describing what's different between
`expected` and `actual`. Empty list means the workbooks match on every
axis.

The scorer is deliberately permissive in one place: cells where the
formula string differs but the evaluated value matches (to within
numeric tolerance) are labeled `formula_drift` rather than a flat
value-mismatch failure. This prevents the suite from failing when a
model generates `=B1+B2` and we expected `=SUM(B1:B2)` for the same
result.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from evals.results.schema import CellDiff
from shotcut.spreadsheet.engine import Engine, FormulaEvaluationError
from shotcut.spreadsheet.workbook import Workbook

NUMERIC_TOLERANCE = 1e-6
MAX_DIFFS_PER_CASE = 20


@dataclass(frozen=True)
class ScoreReport:
    diffs: list[CellDiff]
    diffs_truncated: bool

    @property
    def passed(self) -> bool:
        return not self.diffs


def score(expected: Workbook, actual: Workbook) -> ScoreReport:
    """Compare two workbooks; return the (bounded) diff list."""
    raw_diffs: list[CellDiff] = []
    _score_structure(expected, actual, raw_diffs)
    _score_cells_and_formulas(expected, actual, raw_diffs)

    truncated = len(raw_diffs) > MAX_DIFFS_PER_CASE
    diffs = raw_diffs[:MAX_DIFFS_PER_CASE]
    return ScoreReport(diffs=diffs, diffs_truncated=truncated)


def _score_structure(
    expected: Workbook, actual: Workbook, diffs: list[CellDiff]
) -> None:
    expected_sheets = set(expected.raw.sheetnames)
    actual_sheets = set(actual.raw.sheetnames)

    for missing in sorted(expected_sheets - actual_sheets):
        diffs.append(
            CellDiff(
                axis="structure",
                location=missing,
                expected="sheet_present",
                actual="sheet_missing",
            )
        )
    for extra in sorted(actual_sheets - expected_sheets):
        diffs.append(
            CellDiff(
                axis="structure",
                location=extra,
                expected="sheet_absent",
                actual="sheet_present",
            )
        )


def _score_cells_and_formulas(
    expected: Workbook, actual: Workbook, diffs: list[CellDiff]
) -> None:
    expected_engine = Engine(expected)
    actual_engine = Engine(actual)

    for sheet in expected.raw.sheetnames:
        if sheet not in actual.raw.sheetnames:
            continue  # already recorded as a structural miss
        expected_ws = expected.raw[sheet]
        actual_ws = actual.raw[sheet]

        for row in expected_ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                ref = cell.coordinate
                exp_value = cell.value
                act_cell = actual_ws[ref]
                act_value = act_cell.value

                _compare_cell(
                    sheet=sheet,
                    ref=ref,
                    exp_value=exp_value,
                    act_value=act_value,
                    expected_engine=expected_engine,
                    actual_engine=actual_engine,
                    diffs=diffs,
                )


def _compare_cell(
    *,
    sheet: str,
    ref: str,
    exp_value: Any,
    act_value: Any,
    expected_engine: Engine,
    actual_engine: Engine,
    diffs: list[CellDiff],
) -> None:
    location = f"{sheet}!{ref}"
    exp_is_formula = isinstance(exp_value, str) and exp_value.startswith("=")
    act_is_formula = isinstance(act_value, str) and act_value.startswith("=")

    if exp_is_formula and act_is_formula:
        if exp_value == act_value:
            return
        # Formulas differ. Fall back to evaluated-value comparison.
        if _same_evaluated_value(
            sheet, ref, expected_engine, actual_engine
        ):
            diffs.append(
                CellDiff(
                    axis="formula",
                    location=location,
                    expected=exp_value,
                    actual=act_value,
                    note="formula_drift: same evaluated value",
                )
            )
        else:
            diffs.append(
                CellDiff(
                    axis="formula",
                    location=location,
                    expected=exp_value,
                    actual=act_value,
                )
            )
        return

    if exp_is_formula and not act_is_formula:
        diffs.append(
            CellDiff(
                axis="formula",
                location=location,
                expected=exp_value,
                actual=act_value,
                note="expected a formula, got a literal",
            )
        )
        return

    if not exp_is_formula and act_is_formula:
        diffs.append(
            CellDiff(
                axis="formula",
                location=location,
                expected=exp_value,
                actual=act_value,
                note="expected a literal, got a formula",
            )
        )
        return

    # Both are literals.
    if _values_equal(exp_value, act_value):
        return
    diffs.append(
        CellDiff(
            axis="cell_value",
            location=location,
            expected=exp_value,
            actual=act_value,
        )
    )


def _values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < NUMERIC_TOLERANCE
    return bool(a == b)


def _same_evaluated_value(
    sheet: str,
    ref: str,
    expected_engine: Engine,
    actual_engine: Engine,
) -> bool:
    try:
        exp_eval = expected_engine.evaluate_cell(sheet, ref)
    except FormulaEvaluationError:
        return False
    try:
        act_eval = actual_engine.evaluate_cell(sheet, ref)
    except FormulaEvaluationError:
        return False
    return _values_equal(exp_eval, act_eval)
