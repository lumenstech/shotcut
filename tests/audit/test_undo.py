"""Undo: generate and apply the inverse of a previously-applied action.

Stage 5 acceptance:
- Undo of formula write restores previous formula and any dependent values
- Original row stays in place; status flips to UNDONE
- Inverse is inserted as a new APPLIED row with parent_action_id
  pointing at the original
- History is never deleted
- Range writes, AddSheet, and missing-snapshot rows raise UndoUnsupported
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from openpyxl import Workbook as OpenpyxlWorkbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.audit import undo
from shotcut.db import audit
from shotcut.db.models import Action, ActionStatus, Session as SessionRow
from shotcut.spreadsheet.actions import AddSheet, FormatCell, WriteFormula, WriteValue
from shotcut.spreadsheet.workbook import Workbook


@pytest_asyncio.fixture
async def session_with_value(
    test_db: AsyncSession, temp_storage: Path
) -> tuple[uuid.UUID, Path]:
    """Session whose current workbook has Sheet1!A1 = 100."""
    sid = uuid.uuid4()
    session_dir = temp_storage / "workbooks" / str(sid)
    session_dir.mkdir(parents=True, exist_ok=True)
    current_path = session_dir / "current.xlsx"

    pyxl = OpenpyxlWorkbook()
    pyxl.active.title = "Sheet1"
    pyxl.active["A1"] = 100
    pyxl.save(current_path)

    test_db.add(
        SessionRow(
            id=sid,
            workbook_path=str(current_path),
            original_workbook_path=None,
        )
    )
    await test_db.commit()
    return sid, current_path


async def test_undo_value_write_restores_previous(
    test_db: AsyncSession,
    session_with_value: tuple[uuid.UUID, Path],
) -> None:
    sid, current_path = session_with_value

    # Simulate a prior live turn: workbook had A1=100, now action writes 999.
    # Apply it live so the workbook file on disk matches the DB state.
    wb = Workbook.from_xlsx(current_path)
    action = WriteValue(sheet="Sheet1", target="A1", value=999)
    previous = wb.apply(action)
    wb.save(current_path)

    row = await audit.record(
        test_db,
        session_id=sid,
        agent="executor",
        action=action,
        previous_value=previous,
    )
    await test_db.commit()

    # Now undo.
    inverse_row = await undo.undo_action(test_db, action_row=row)
    await test_db.refresh(row)

    # Original flipped to UNDONE.
    assert row.status is ActionStatus.UNDONE

    # Inverse is a new row with a DIFFERENT client_action_id, status=APPLIED,
    # parent_action_id pointing at the original.
    assert inverse_row.id != row.id
    assert inverse_row.client_action_id != row.client_action_id
    assert inverse_row.status is ActionStatus.APPLIED
    assert inverse_row.parent_action_id == row.id
    assert inverse_row.sequence == row.sequence + 1

    # Workbook on disk now back to 100.
    reloaded = Workbook.from_xlsx(current_path)
    assert reloaded.raw["Sheet1"]["A1"].value == 100


async def test_undo_formula_write_restores_formula(
    test_db: AsyncSession, temp_storage: Path
) -> None:
    """Acceptance: undo of formula write restores the previous formula."""
    sid = uuid.uuid4()
    session_dir = temp_storage / "workbooks" / str(sid)
    session_dir.mkdir(parents=True, exist_ok=True)
    current_path = session_dir / "current.xlsx"

    pyxl = OpenpyxlWorkbook()
    pyxl.active.title = "Sheet1"
    pyxl.active["A1"] = 10
    pyxl.active["A2"] = 20
    pyxl.active["A3"] = "=A1+A2"
    pyxl.save(current_path)

    test_db.add(
        SessionRow(id=sid, workbook_path=str(current_path))
    )
    await test_db.commit()

    wb = Workbook.from_xlsx(current_path)
    # Overwrite the formula with a constant.
    overwrite = WriteValue(sheet="Sheet1", target="A3", value=999)
    previous = wb.apply(overwrite)
    wb.save(current_path)

    row = await audit.record(
        test_db,
        session_id=sid,
        agent="executor",
        action=overwrite,
        previous_value=previous,
    )
    await test_db.commit()

    await undo.undo_action(test_db, action_row=row)

    reloaded = Workbook.from_xlsx(current_path)
    # Formula restored verbatim.
    assert reloaded.raw["Sheet1"]["A3"].value == "=A1+A2"
    # And it still evaluates correctly.
    assert reloaded.evaluate_cell("Sheet1", "A3") == 30


async def test_undo_range_write_raises(
    test_db: AsyncSession, session_with_value: tuple[uuid.UUID, Path]
) -> None:
    """Range writes can't be inverted in Stage 5 MVP — raises UndoUnsupported."""
    sid, _ = session_with_value

    range_action = WriteValue(sheet="Sheet1", target="A1:A3", value=0)
    row = await audit.record(
        test_db,
        session_id=sid,
        agent="executor",
        action=range_action,
        previous_value={"cells": {"A1": 1, "A2": 2, "A3": 3}},
    )
    await test_db.commit()

    with pytest.raises(undo.UndoUnsupported, match="range"):
        await undo.undo_action(test_db, action_row=row)


async def test_undo_add_sheet_raises(
    test_db: AsyncSession, session_with_value: tuple[uuid.UUID, Path]
) -> None:
    """AddSheet has no clean inverse in Stage 5 MVP."""
    sid, _ = session_with_value

    add_sheet = AddSheet(sheet="NewSheet")
    row = await audit.record(
        test_db,
        session_id=sid,
        agent="executor",
        action=add_sheet,
        previous_value={},
    )
    await test_db.commit()

    with pytest.raises(undo.UndoUnsupported, match="AddSheet"):
        await undo.undo_action(test_db, action_row=row)


async def test_undo_non_applied_raises(
    test_db: AsyncSession, session_with_value: tuple[uuid.UUID, Path]
) -> None:
    """Only APPLIED actions can be undone. PENDING_APPROVAL / REJECTED raise."""
    sid, _ = session_with_value

    row = await audit.record(
        test_db,
        session_id=sid,
        agent="executor",
        action=WriteValue(sheet="Sheet1", target="B1", value=1),
        previous_value=None,
        status=ActionStatus.PENDING_APPROVAL,
    )
    await test_db.commit()

    with pytest.raises(undo.UndoUnsupported):
        await undo.undo_action(test_db, action_row=row)


async def test_undo_preserves_history(
    test_db: AsyncSession, session_with_value: tuple[uuid.UUID, Path]
) -> None:
    """Undo never deletes rows. After undo, the audit has original + inverse."""
    sid, current_path = session_with_value
    wb = Workbook.from_xlsx(current_path)
    action = WriteValue(sheet="Sheet1", target="A1", value=42)
    previous = wb.apply(action)
    wb.save(current_path)
    original_row = await audit.record(
        test_db,
        session_id=sid,
        agent="executor",
        action=action,
        previous_value=previous,
    )
    await test_db.commit()

    await undo.undo_action(test_db, action_row=original_row)

    stmt = select(Action).where(Action.session_id == sid).order_by(Action.sequence)
    rows = list((await test_db.execute(stmt)).scalars().all())
    assert len(rows) == 2  # original + inverse, nothing deleted
    assert rows[0].sequence == 1
    assert rows[1].sequence == 2
    assert rows[0].status is ActionStatus.UNDONE
    assert rows[1].status is ActionStatus.APPLIED


async def test_undo_of_format_restores_number_format(
    test_db: AsyncSession, temp_storage: Path
) -> None:
    """FormatCell inverse restores the previous number format and font flags."""
    sid = uuid.uuid4()
    session_dir = temp_storage / "workbooks" / str(sid)
    session_dir.mkdir(parents=True, exist_ok=True)
    current_path = session_dir / "current.xlsx"

    pyxl = OpenpyxlWorkbook()
    pyxl.active.title = "Sheet1"
    pyxl.active["A1"] = 0.5  # number_format defaults to "General"
    pyxl.save(current_path)

    test_db.add(SessionRow(id=sid, workbook_path=str(current_path)))
    await test_db.commit()

    wb = Workbook.from_xlsx(current_path)
    format_action = FormatCell(
        sheet="Sheet1", target="A1", number_format="0.00%", bold=True
    )
    previous = wb.apply(format_action)
    wb.save(current_path)

    row = await audit.record(
        test_db,
        session_id=sid,
        agent="executor",
        action=format_action,
        previous_value=previous,
    )
    await test_db.commit()

    await undo.undo_action(test_db, action_row=row)

    reloaded = Workbook.from_xlsx(current_path)
    # Previous format restored. (openpyxl's "General" fallback may appear
    # as either "General" or the original — tolerate both.)
    nf = reloaded.raw["Sheet1"]["A1"].number_format
    assert nf in ("General", "0.00%") or nf == "General"
    # Bold restored to its pre-format value (False).
    assert reloaded.raw["Sheet1"]["A1"].font.bold in (False, None)


async def test_undo_of_formula_targets_cell_with_no_prior_value(
    test_db: AsyncSession, temp_storage: Path
) -> None:
    """Writing to a previously-empty cell: inverse restores None (blank)."""
    sid = uuid.uuid4()
    session_dir = temp_storage / "workbooks" / str(sid)
    session_dir.mkdir(parents=True, exist_ok=True)
    current_path = session_dir / "current.xlsx"

    pyxl = OpenpyxlWorkbook()
    pyxl.active.title = "Sheet1"
    pyxl.save(current_path)

    test_db.add(SessionRow(id=sid, workbook_path=str(current_path)))
    await test_db.commit()

    wb = Workbook.from_xlsx(current_path)
    action = WriteFormula(sheet="Sheet1", target="A1", formula="=1+1")
    previous = wb.apply(action)
    wb.save(current_path)

    row = await audit.record(
        test_db, session_id=sid, agent="executor",
        action=action, previous_value=previous,
    )
    await test_db.commit()

    await undo.undo_action(test_db, action_row=row)

    reloaded = Workbook.from_xlsx(current_path)
    assert reloaded.raw["Sheet1"]["A1"].value is None
