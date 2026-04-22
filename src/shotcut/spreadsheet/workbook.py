"""Thin wrapper around openpyxl that applies Action objects and surfaces
workbook state to agents as compact JSON.

Scope: MVP. openpyxl does not evaluate formulas; we rely on Excel to
recalculate on open. Formula *syntax* validation lives in engine.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openpyxl import Workbook as OpenpyxlWorkbook
from openpyxl import load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import coordinate_from_string, range_boundaries

from shotcut.spreadsheet.actions import (
    Action,
    AddSheet,
    FormatCell,
    SetColumnWidth,
    WriteFormula,
    WriteValue,
)


@dataclass
class CellSnapshot:
    value: Any
    formula: str | None
    number_format: str | None


class Workbook:
    def __init__(self, wb: OpenpyxlWorkbook):
        self._wb = wb

    @classmethod
    def load(cls, path: Path) -> Workbook:
        return cls(load_workbook(path))

    @classmethod
    def blank(cls) -> Workbook:
        wb = OpenpyxlWorkbook()
        # Openpyxl starts with one default sheet named "Sheet".
        return cls(wb)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._wb.save(path)

    # --- context summary for agents ---

    def summary(self, max_cells_per_sheet: int = 50) -> dict:
        """Compact JSON-friendly snapshot for prompt context."""
        sheets = []
        for name in self._wb.sheetnames:
            ws = self._wb[name]
            cells: list[dict] = []
            for row in ws.iter_rows():
                for cell in row:
                    if cell.value is None:
                        continue
                    cells.append(
                        {
                            "ref": cell.coordinate,
                            "value": cell.value,
                            "number_format": cell.number_format or None,
                        }
                    )
                    if len(cells) >= max_cells_per_sheet:
                        break
                if len(cells) >= max_cells_per_sheet:
                    break
            sheets.append(
                {
                    "name": name,
                    "dimensions": ws.dimensions,
                    "cells": cells,
                    "truncated": len(cells) >= max_cells_per_sheet,
                }
            )
        return {"sheets": sheets}

    # --- apply actions ---

    def apply(self, action: Action) -> dict | None:
        """Apply an action and return the previous value snapshot (for audit)."""
        if isinstance(action, WriteFormula):
            return self._write(action.sheet, action.target, action.formula)
        if isinstance(action, WriteValue):
            return self._write(action.sheet, action.target, action.value)
        if isinstance(action, FormatCell):
            return self._format(action)
        if isinstance(action, AddSheet):
            self._wb.create_sheet(action.sheet)
            return None
        if isinstance(action, SetColumnWidth):
            ws = self._wb[action.sheet]
            col_letter = self._first_column_letter(action.target)
            prev = ws.column_dimensions[col_letter].width
            ws.column_dimensions[col_letter].width = action.width
            return {"width": prev}
        raise ValueError(f"Unsupported action: {action!r}")

    # --- internals ---

    def _write(self, sheet: str, target: str, value: Any) -> dict:
        ws = self._wb[sheet]
        previous = {}
        if ":" in target:
            min_col, min_row, max_col, max_row = range_boundaries(target)
            for row in range(min_row, max_row + 1):
                for col in range(min_col, max_col + 1):
                    cell = ws.cell(row=row, column=col)
                    previous[cell.coordinate] = cell.value
                    cell.value = value
        else:
            col, row = coordinate_from_string(target)
            cell = ws[f"{col}{row}"]
            previous[cell.coordinate] = cell.value
            cell.value = value
        return {"cells": previous}

    def _format(self, action: FormatCell) -> dict:
        ws = self._wb[action.sheet]
        previous: dict = {}

        if ":" in action.target:
            min_col, min_row, max_col, max_row = range_boundaries(action.target)
            cells = [
                ws.cell(row=r, column=c)
                for r in range(min_row, max_row + 1)
                for c in range(min_col, max_col + 1)
            ]
        else:
            cells = [ws[action.target]]

        for cell in cells:
            previous[cell.coordinate] = {
                "number_format": cell.number_format,
                "font": {"bold": cell.font.bold, "italic": cell.font.italic},
            }
            if action.number_format is not None:
                cell.number_format = action.number_format
            if action.bold is not None or action.italic is not None:
                cell.font = Font(
                    bold=action.bold if action.bold is not None else cell.font.bold,
                    italic=action.italic if action.italic is not None else cell.font.italic,
                )
        return previous

    @staticmethod
    def _first_column_letter(target: str) -> str:
        if ":" in target:
            min_col, _, _, _ = range_boundaries(target)
            return get_column_letter(min_col)
        col, _ = coordinate_from_string(target)
        return col
