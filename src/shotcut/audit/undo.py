"""Undo: generate and apply the inverse of a previously-applied action.

Semantic chosen (see docs/decisions/0002-schema-consolidation.md →
Stage 5 erratum and the ActionStatus state machine diagram):

- The original action row stays in place; its `status` flips from
  APPLIED to UNDONE. Replay skips nothing — UNDONE rows are still
  replayed because they *were* applied to the live workbook.
- A new Action row is inserted with status=APPLIED, a fresh
  `client_action_id` (the inverse is its own logical action), and
  `parent_action_id` pointing at the original so audit UIs can render
  the relationship.
- The inverse action is applied to the current workbook, saved back
  to `session.workbook_path`, and that net state is what the user sees.

Range writes and AddSheet currently don't have clean inverses. The
former would need a multi-cell restore; the latter a RemoveSheet
action type we haven't implemented. Calling `undo()` on either raises
`UndoUnsupported` so callers surface it as a 409.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from shotcut.db import audit
from shotcut.db.models import Action as ActionRow
from shotcut.db.models import ActionStatus, Session as SessionRow
from shotcut.spreadsheet.actions import (
    Action as AgentAction,
    FormatCell,
    SetColumnWidth,
    WriteFormula,
    WriteValue,
)
from shotcut.spreadsheet.workbook import Workbook
from shotcut.storage import get_storage
from sqlalchemy.ext.asyncio import AsyncSession


class UndoUnsupported(Exception):
    """The original action doesn't have a clean inverse (range write,
    AddSheet, or a row missing its previous_value snapshot)."""


async def undo_action(db: AsyncSession, *, action_row: ActionRow) -> ActionRow:
    """Generate and apply the inverse of `action_row`.

    Returns the newly-inserted inverse ActionRow (status=APPLIED,
    parent_action_id=action_row.id).
    """
    if action_row.status is not ActionStatus.APPLIED:
        raise UndoUnsupported(
            f"action is {action_row.status.value}; only APPLIED actions can be undone"
        )

    session_row = await db.get(SessionRow, action_row.session_id)
    if session_row is None:
        raise ValueError(f"session {action_row.session_id} no longer exists")

    inverse = _build_inverse(action_row)

    # Apply to the current workbook.
    workbook_path = Path(session_row.workbook_path)
    workbook = (
        Workbook.from_xlsx(workbook_path) if workbook_path.exists() else Workbook.blank()
    )
    previous = workbook.apply(inverse)

    # Save back to storage.
    storage = get_storage()
    current_path = storage.local_path(session_row.id, "current.xlsx")
    assert current_path is not None
    workbook.save(current_path)

    # Record the inverse as a fresh APPLIED action with parent_action_id.
    inverse_row = await audit.record(
        db,
        session_id=session_row.id,
        agent="undo",
        action=inverse,
        previous_value=previous,
        reasoning=f"undo of action {action_row.id}",
    )
    inverse_row.parent_action_id = action_row.id

    # Flip the original's status to UNDONE (informational; replay still
    # applies it — the inverse's effect follows later in the sequence).
    action_row.status = ActionStatus.UNDONE
    session_row.workbook_path = str(current_path)
    await db.commit()
    await db.refresh(inverse_row)
    return inverse_row


def _build_inverse(row: ActionRow) -> AgentAction:
    """Construct an inverse Action for the given row.

    Uses `row.previous_value` captured at apply time. See `Workbook._write`
    and `Workbook._format` for the exact snapshot shape those routines
    produce.
    """
    prev = row.previous_value
    if prev is None:
        raise UndoUnsupported(
            f"action {row.id} has no previous_value snapshot; cannot invert"
        )

    action_type = row.action_type
    sheet = row.sheet
    target = row.target_range
    if sheet is None or target is None:
        raise UndoUnsupported(
            f"action {row.id} lacks sheet/target; cannot invert"
        )

    if ":" in target:
        raise UndoUnsupported(
            "range writes are not invertible in Stage 5 MVP; cell-by-cell "
            "restoration would need a bulk-write action type"
        )

    if action_type in {"write_value", "write_formula"}:
        # _write() snapshots `{"cells": {coord: prev_value}}`.
        cells = prev.get("cells", {})
        restored_value: Any = cells.get(target)
        # If the restored value is a formula string, use WriteFormula;
        # otherwise WriteValue handles all literal kinds.
        if isinstance(restored_value, str) and restored_value.startswith("="):
            return WriteFormula(sheet=sheet, target=target, formula=restored_value)
        return WriteValue(sheet=sheet, target=target, value=restored_value)

    if action_type == "format_cell":
        # _format() snapshots `{coord: {"number_format": ..., "font": {...}}}`.
        cell_snapshot = prev.get(target, {})
        font = cell_snapshot.get("font", {}) or {}
        return FormatCell(
            sheet=sheet,
            target=target,
            number_format=cell_snapshot.get("number_format"),
            bold=font.get("bold"),
            italic=font.get("italic"),
        )

    if action_type == "set_column_width":
        # _apply_set_column_width snapshots `{"width": prev_width}`.
        return SetColumnWidth(
            sheet=sheet, target=target, width=prev.get("width", 0.0) or 0.0
        )

    if action_type == "add_sheet":
        raise UndoUnsupported(
            "AddSheet has no inverse; removing a sheet would destroy "
            "state on cells the sheet contains"
        )

    raise UndoUnsupported(f"unknown action_type {action_type!r}")


# Re-export so the orchestrator / API can catch a single exception name.
__all__ = ["UndoUnsupported", "undo_action"]
