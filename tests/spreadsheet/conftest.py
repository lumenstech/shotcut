"""Fixture workbook generators for parser/engine round-trip tests.

Each fixture builds an `.xlsx` on disk in `tmp_path` using openpyxl
primitives. This keeps the git repo small (no binary fixtures) and makes
the feature coverage self-documenting — if you want to know what
"feature_rich_xlsx" exercises, read the builder.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook as OpenpyxlWorkbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import PatternFill
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation


@pytest.fixture
def feature_rich_xlsx(tmp_path: Path) -> Path:
    """Build an .xlsx exercising everything the parser is required to preserve:

    - Multi-sheet with at least one hidden sheet
    - Cell values AND formulas (to guard against data_only stripping)
    - Named range (workbook-scoped)
    - Merged cell range
    - Data validation (list)
    - Conditional formatting (cellIs)
    - Sheet protection flag on one sheet
    - Cross-sheet formula reference
    """
    wb = OpenpyxlWorkbook()

    # --- Sheet1: Inputs ---
    ws1 = wb.active
    ws1.title = "Inputs"
    ws1["A1"] = "Revenue"
    ws1["B1"] = 1000.0
    ws1["A2"] = "Growth"
    ws1["B2"] = 0.1
    ws1["A3"] = "Cost Ratio"
    ws1["B3"] = 0.4
    # Merged title
    ws1["A5"] = "Model Inputs"
    ws1.merge_cells("A5:B5")
    # Data validation: B2 must be a decimal between 0 and 1
    dv = DataValidation(
        type="decimal",
        operator="between",
        formula1=0,
        formula2=1,
        allow_blank=True,
    )
    dv.add("B2")
    ws1.add_data_validation(dv)
    # Conditional formatting: highlight B3 if > 0.5
    red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    ws1.conditional_formatting.add(
        "B3",
        CellIsRule(operator="greaterThan", formula=["0.5"], fill=red_fill),
    )

    # --- Sheet2: Model (hidden) ---
    ws2 = wb.create_sheet("Model")
    ws2.sheet_state = "hidden"
    ws2["A1"] = "Year"
    ws2["B1"] = "Revenue"
    ws2["C1"] = "Cost"
    ws2["D1"] = "Profit"
    for year_offset in range(3):
        row = 2 + year_offset
        ws2[f"A{row}"] = 2024 + year_offset
        # Formula referencing Inputs sheet
        ws2[f"B{row}"] = f"=Inputs!$B$1*(1+Inputs!$B$2)^{year_offset}"
        ws2[f"C{row}"] = f"=B{row}*Inputs!$B$3"
        ws2[f"D{row}"] = f"=B{row}-C{row}"
    ws2["A5"] = "Total"
    ws2["D5"] = "=SUM(D2:D4)"

    # --- Sheet3: Summary (protected) ---
    ws3 = wb.create_sheet("Summary")
    ws3["A1"] = "Total Profit"
    ws3["B1"] = "=Model!D5"
    ws3.protection.sheet = True

    # Named range (workbook-scoped)
    wb.defined_names["Revenue"] = DefinedName("Revenue", attr_text="Inputs!$B$1")

    path = tmp_path / "feature_rich.xlsx"
    wb.save(path)
    return path


@pytest.fixture
def simple_xlsx(tmp_path: Path) -> Path:
    """Minimal .xlsx for size/scan tests."""
    wb = OpenpyxlWorkbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = 1.0
    path = tmp_path / "simple.xlsx"
    wb.save(path)
    return path
