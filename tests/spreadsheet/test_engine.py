"""Stage 1 acceptance tests for the calc engine.

Covers:
  - Round-trip: write SUM, evaluate, assert value
  - Cross-sheet reference resolution
  - Circular reference detection
  - 20 ground-truth formulas from tests/fixtures/formulas/excel_truth.json
  - Unsupported-function error reporting
  - Cache invalidation: mutating a cell re-evaluates dependents
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shotcut.spreadsheet.actions import AddSheet, WriteFormula, WriteValue
from shotcut.spreadsheet.engine import (
    CircularReferenceError,
    UnsupportedFormulaError,
)
from shotcut.spreadsheet.workbook import Workbook


FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "formulas" / "excel_truth.json"
NUMERIC_TOLERANCE = 1e-6


# ---------------------------------------------------------------------------
# Specific acceptance criteria
# ---------------------------------------------------------------------------


def test_sum_roundtrip():
    """Write =SUM(A1:A5) over 1..5, evaluate → 15."""
    wb = Workbook.blank()
    for i in range(1, 6):
        wb.apply(WriteValue(sheet="Sheet", target=f"A{i}", value=float(i)))
    wb.apply(WriteFormula(sheet="Sheet", target="B1", formula="=SUM(A1:A5)"))
    assert wb.evaluate_cell("Sheet", "B1") == 15.0


def test_cross_sheet_reference():
    """Sheet1!A1 = Sheet2!B2 + 10 resolves correctly."""
    wb = Workbook.blank()
    # Blank workbooks start with one sheet named "Sheet"; add two more.
    wb.apply(AddSheet(sheet="Sheet1"))
    wb.apply(AddSheet(sheet="Sheet2"))
    wb.apply(WriteValue(sheet="Sheet2", target="B2", value=32.0))
    wb.apply(WriteFormula(sheet="Sheet1", target="A1", formula="=Sheet2!B2 + 10"))
    assert wb.evaluate_cell("Sheet1", "A1") == 42.0


def test_cycle_detection_raises():
    """A1=B1, B1=A1 → raises CircularReferenceError."""
    wb = Workbook.blank()
    wb.apply(WriteFormula(sheet="Sheet", target="A1", formula="=B1"))
    wb.apply(WriteFormula(sheet="Sheet", target="B1", formula="=A1"))
    with pytest.raises(CircularReferenceError):
        wb.evaluate_cell("Sheet", "A1")


# ---------------------------------------------------------------------------
# Ground-truth fixture suite
# ---------------------------------------------------------------------------


def _load_fixture_cases() -> list[dict]:
    data = json.loads(FIXTURE_PATH.read_text())
    return data["cases"]


@pytest.mark.parametrize("case", _load_fixture_cases(), ids=lambda c: c["id"])
def test_excel_truth(case: dict):
    """Run one ground-truth formula and assert the evaluated value matches."""
    wb = Workbook.blank()

    # Create declared sheets. Some cases use the default "Sheet"; some need
    # named sheets. Both paths go through apply() so the engine sees them.
    default_sheet_needed = any(
        s == "Sheet" for s in case["sheets"]
    ) or any(
        inp["sheet"] == "Sheet" for inp in case["inputs"]
    )
    if not default_sheet_needed:
        # The blank workbook's default "Sheet" is unused in this case;
        # openpyxl leaves it empty, which is fine.
        pass

    for sheet_name in case["sheets"]:
        if sheet_name == "Sheet":
            continue  # already exists on blank workbook
        wb.apply(AddSheet(sheet=sheet_name))

    for inp in case["inputs"]:
        wb.apply(
            WriteValue(
                sheet=inp["sheet"],
                target=inp["ref"],
                value=inp["value"],
            )
        )

    formula_cell = case["formula_cell"]
    wb.apply(
        WriteFormula(
            sheet=formula_cell["sheet"],
            target=formula_cell["ref"],
            formula=case["formula"],
        )
    )

    actual = wb.evaluate_cell(formula_cell["sheet"], formula_cell["ref"])
    expected = case["expected"]

    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        assert abs(actual - expected) < NUMERIC_TOLERANCE, (
            f"{case['id']}: expected {expected}, got {actual}"
        )
    else:
        assert actual == expected, (
            f"{case['id']}: expected {expected!r}, got {actual!r}"
        )


def test_fixture_has_20_cases():
    """The BUILD_PLAN calls for 20 ground-truth formulas."""
    assert len(_load_fixture_cases()) == 20


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def test_unsupported_function_raises():
    """Calling an unknown function produces UnsupportedFormulaError."""
    wb = Workbook.blank()
    wb.apply(WriteFormula(sheet="Sheet", target="A1", formula="=BOGUSFN(1,2,3)"))
    with pytest.raises(UnsupportedFormulaError) as exc_info:
        wb.evaluate_cell("Sheet", "A1")
    # Best-effort function-name extraction from the formula string.
    assert exc_info.value.function_name == "BOGUSFN"


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------


def test_cache_invalidated_on_write():
    """Mutating a cell after an evaluation re-evaluates dependents."""
    wb = Workbook.blank()
    wb.apply(WriteValue(sheet="Sheet", target="A1", value=1.0))
    wb.apply(WriteFormula(sheet="Sheet", target="B1", formula="=A1*10"))
    assert wb.evaluate_cell("Sheet", "B1") == 10.0

    wb.apply(WriteValue(sheet="Sheet", target="A1", value=7.0))
    assert wb.evaluate_cell("Sheet", "B1") == 70.0


def test_evaluate_cell_returns_literal_for_value_cell():
    """evaluate_cell on a non-formula cell returns the literal value."""
    wb = Workbook.blank()
    wb.apply(WriteValue(sheet="Sheet", target="A1", value=42.0))
    assert wb.evaluate_cell("Sheet", "A1") == 42.0


def test_evaluate_cell_returns_none_for_empty_cell():
    """Empty cells return None — callers disambiguate from literal None inputs."""
    wb = Workbook.blank()
    # No writes; Sheet!A1 is empty.
    result = wb.evaluate_cell("Sheet", "A1")
    assert result is None
