"""Formula evaluation engine.

Thin wrapper around `formualizer` (Rust core, PyO3 bindings, abi3 wheel).
Responsible for:

- Mirroring `Workbook` state (cell values + formulas, sheet by sheet) into
  a `formualizer.Workbook` on demand.
- Translating openpyxl's A1-string coordinate system to formualizer's
  1-based (row, col) pair at the API seam.
- Converting formualizer's in-band Excel errors
  (`{"type": "Error", "kind": "Circ" | "Ref" | "Name" | ...}`) into typed
  Python exceptions the verifier can classify.
- Caching: formualizer already caches internally, so we track a single
  "dirty" flag and rebuild from scratch after workbook mutations. For the
  workbook sizes we expect (low thousands of cells), a full rebuild is
  milliseconds and a saner mental model than tracking per-cell deltas.

Design decisions recorded in docs/decisions/0001-calc-engine.md.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import formualizer as fz
from openpyxl.utils.cell import coordinate_from_string, column_index_from_string

if TYPE_CHECKING:
    from shotcut.spreadsheet.workbook import Workbook


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class FormulaEvaluationError(Exception):
    """Base class for all evaluation-time errors."""


class CircularReferenceError(FormulaEvaluationError):
    """Dependency graph contains a cycle (Excel `#CIRC!`)."""


class UnsupportedFormulaError(FormulaEvaluationError):
    """Formula uses a function formualizer does not implement (Excel `#NAME?`).

    `function_name` is best-effort — parsed out of the formula string. None
    if we can't identify which function tripped the error (e.g. name errors
    from unresolved named ranges).
    """

    def __init__(self, function_name: str | None, formula: str) -> None:
        self.function_name = function_name
        self.formula = formula
        msg = (
            f"unsupported function {function_name!r} in formula {formula!r}"
            if function_name
            else f"unresolved name in formula {formula!r}"
        )
        super().__init__(msg)


class InvalidReferenceError(FormulaEvaluationError):
    """Reference to a non-existent cell or range (Excel `#REF!`)."""


class InvalidValueError(FormulaEvaluationError):
    """Type mismatch or invalid argument (Excel `#VALUE!`)."""


class DivisionByZeroError(FormulaEvaluationError):
    """Division by zero (Excel `#DIV/0!`)."""


class NotAvailableError(FormulaEvaluationError):
    """Lookup returned no match (Excel `#N/A`)."""


class NumericError(FormulaEvaluationError):
    """Numeric overflow or invalid numeric operation (Excel `#NUM!`)."""


# Map formualizer's `kind` strings to exception classes. Keys are normalized
# to lowercase so we can be lenient about case.
_ERROR_MAP: dict[str, type[FormulaEvaluationError]] = {
    "circ": CircularReferenceError,
    "ref": InvalidReferenceError,
    "value": InvalidValueError,
    "div": DivisionByZeroError,
    "na": NotAvailableError,
    "num": NumericError,
}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


# Matches the outermost function name in an Excel formula, e.g.
# "=MYFUNC(A1)" → "MYFUNC". Conservative; only catches the first one.
_FUNCTION_NAME_RE = re.compile(r"([A-Z][A-Z0-9_.]*)\s*\(")


class Engine:
    """Evaluator bound to a single `Workbook` instance.

    The engine is created lazily by the workbook; callers should use
    `Workbook.evaluate_cell` rather than constructing this directly.
    """

    def __init__(self, workbook: Workbook) -> None:
        self._workbook = workbook
        self._fz: fz.Workbook | None = None
        self._dirty = True

    def mark_dirty(self) -> None:
        """Called by `Workbook.apply` after a mutation."""
        self._dirty = True

    def evaluate_cell(self, sheet: str, ref: str) -> Any:
        """Evaluate the cell at `sheet!ref` and return its value.

        `ref` is an A1-style coordinate (e.g. "B3"). Returns the computed
        numeric/string/boolean/date value. On cells containing literal
        values (not formulas), returns the literal. On formula errors,
        raises the appropriate `FormulaEvaluationError` subclass.
        """
        self._ensure_synced()
        assert self._fz is not None
        col_letter, row = coordinate_from_string(ref)
        col = column_index_from_string(col_letter)
        result = self._fz.evaluate_cell(sheet, row, col)
        return self._unwrap(result, sheet=sheet, ref=ref)

    # --- internals ---

    def _ensure_synced(self) -> None:
        if not self._dirty and self._fz is not None:
            return
        self._fz = self._build_fz_workbook()
        self._dirty = False

    def _build_fz_workbook(self) -> fz.Workbook:
        """Mirror the openpyxl-backed Workbook into a fresh formualizer workbook.

        Full rebuild rather than incremental sync — simpler, and fast enough
        for the cell counts we handle. Revisit if profiling says otherwise.
        """
        fz_wb = fz.Workbook()
        openpyxl_wb = self._workbook.raw
        for sheet_name in openpyxl_wb.sheetnames:
            ws = openpyxl_wb[sheet_name]
            fz_sheet = fz_wb.sheet(sheet_name)
            for row in ws.iter_rows():
                for cell in row:
                    value = cell.value
                    if value is None:
                        continue
                    self._set_cell(fz_sheet, cell.row, cell.column, value)
        return fz_wb

    @staticmethod
    def _set_cell(fz_sheet: Any, row: int, col: int, value: Any) -> None:
        if isinstance(value, str) and value.startswith("="):
            fz_sheet.set_formula(row, col, value)
        elif isinstance(value, bool):
            fz_sheet.set_value(row, col, fz.LiteralValue.boolean(value))
        elif isinstance(value, (int, float)):
            fz_sheet.set_value(row, col, fz.LiteralValue.number(float(value)))
        elif isinstance(value, str):
            fz_sheet.set_value(row, col, fz.LiteralValue.text(value))
        else:
            # Dates, datetimes, timedeltas: coerce to string via Excel
            # semantics. For Stage 1 we accept the fidelity hit; Stage 2
            # ingestion preserves originals via load_workbook.
            fz_sheet.set_value(row, col, fz.LiteralValue.text(str(value)))

    def _unwrap(self, result: Any, *, sheet: str, ref: str) -> Any:
        """Translate formualizer's output into Python values or exceptions."""
        if isinstance(result, dict) and result.get("type") == "Error":
            raise self._error_to_exception(result, sheet=sheet, ref=ref)
        return result

    def _error_to_exception(
        self, error: dict[str, Any], *, sheet: str, ref: str
    ) -> FormulaEvaluationError:
        kind = str(error.get("kind", "")).lower()
        exc_cls = _ERROR_MAP.get(kind)
        if exc_cls is not None:
            return exc_cls(f"{sheet}!{ref}: {error}")
        if kind == "name":
            # #NAME? — most often an unsupported function, occasionally an
            # unresolved named range. Best-effort extract the function name
            # from the original formula text.
            openpyxl_wb = self._workbook.raw
            cell_value = openpyxl_wb[sheet][ref].value
            formula = cell_value if isinstance(cell_value, str) else ""
            match = _FUNCTION_NAME_RE.search(formula) if formula else None
            function_name = match.group(1) if match else None
            return UnsupportedFormulaError(function_name, formula)
        # Unknown error kind — surface as the base class so nothing is silently lost.
        return FormulaEvaluationError(f"{sheet}!{ref}: {error}")
