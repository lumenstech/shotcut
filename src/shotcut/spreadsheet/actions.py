"""Structured action types emitted by the executor agent.

Each action is an atomic mutation the orchestrator applies to a workbook and
records in the audit log. Actions are intentionally narrow — the executor
works at the level of a single cell or range, and the LLM emits them via
tool use.

`client_action_id` is the stable identity of a logical action across
processes, replays, and Stage 5 branches — see
docs/decisions/0002-schema-consolidation.md → "Stage 5 erratum". It
defaults to a fresh UUID at construction; callers can pass one explicitly
to preserve identity across a serialize/deserialize round-trip (e.g.
when the orchestrator reconstructs actions from DB rows for replay).
"""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class _Base(BaseModel):
    client_action_id: UUID = Field(default_factory=uuid4)
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
    client_action_id: UUID = Field(default_factory=uuid4)
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
