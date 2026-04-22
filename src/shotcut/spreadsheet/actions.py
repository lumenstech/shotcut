"""Structured action types emitted by the executor agent.

Each action is an atomic mutation the orchestrator applies to a workbook and
records in the audit log. Actions are intentionally narrow — the executor
works at the level of a single cell or range, and the LLM emits them via
tool use.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class _Base(BaseModel):
    sheet: str
    target: str  # A1 reference or range, e.g. "B2" or "A1:D10"


class WriteFormula(_Base):
    type: Literal["write_formula"] = "write_formula"
    formula: str  # must start with "="


class WriteValue(_Base):
    type: Literal["write_value"] = "write_value"
    value: str | float | int | bool | None


class FormatCell(_Base):
    type: Literal["format_cell"] = "format_cell"
    number_format: str | None = None  # e.g. "$#,##0.00", "0.00%"
    bold: bool | None = None
    italic: bool | None = None


class AddSheet(BaseModel):
    type: Literal["add_sheet"] = "add_sheet"
    sheet: str
    target: str = "A1"  # unused; kept for uniform logging


class SetColumnWidth(_Base):
    type: Literal["set_column_width"] = "set_column_width"
    width: float


Action = Annotated[
    WriteFormula | WriteValue | FormatCell | AddSheet | SetColumnWidth,
    Field(discriminator="type"),
]
