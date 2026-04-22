"""Replay: reconstruct workbook state at an arbitrary sequence point.

Stage 5 acceptance criteria covered here:
- 100-action session replays to identical workbook state
- Replay up to sequence N matches the workbook as it existed after
  action N was applied
- PENDING_APPROVAL and REJECTED rows are skipped (they never mutated
  the workbook); APPLIED and UNDONE are replayed
- `locate_action_by_client_id` finds an action in any branch it exists in
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.audit import replay
from shotcut.db import audit
from shotcut.db.models import ActionStatus
from shotcut.db.models import Session as SessionRow
from shotcut.spreadsheet.actions import AddSheet, WriteFormula, WriteValue


@pytest_asyncio.fixture
async def session_id(test_db: AsyncSession, temp_storage: Path) -> uuid.UUID:
    sid = uuid.uuid4()
    test_db.add(
        SessionRow(
            id=sid,
            workbook_path=str(temp_storage / "workbooks" / str(sid) / "current.xlsx"),
            original_workbook_path=None,
        )
    )
    await test_db.commit()
    return sid


async def test_replay_hundred_actions_matches_live_workbook(
    test_db: AsyncSession, session_id: uuid.UUID, temp_storage: Path
) -> None:
    """The acceptance criterion: 100-action session replays identically."""
    from shotcut.spreadsheet.workbook import Workbook

    live = Workbook.blank()
    # The blank workbook has a "Sheet" — make it consistent across live and replay.
    for i in range(1, 101):
        action = WriteValue(sheet="Sheet", target=f"A{i}", value=float(i * 3))
        await audit.record(
            test_db,
            session_id=session_id,
            agent="executor",
            action=action,
            previous_value=None,
        )
        live.apply(action)
    await test_db.commit()

    replayed = await replay.replay_to(test_db, session_id=session_id)

    live_summary = {
        (s["name"], c["ref"]): c["value"]
        for s in live.summary(max_cells_per_sheet=10_000)["sheets"]
        for c in s["cells"]
    }
    replayed_summary = {
        (s["name"], c["ref"]): c["value"]
        for s in replayed.summary(max_cells_per_sheet=10_000)["sheets"]
        for c in s["cells"]
    }
    assert replayed_summary == live_summary


async def test_replay_to_intermediate_sequence(
    test_db: AsyncSession, session_id: uuid.UUID
) -> None:
    """replay up to sequence N = 3 stops applying after the 3rd action."""
    for i in range(1, 11):
        await audit.record(
            test_db,
            session_id=session_id,
            agent="executor",
            action=WriteValue(sheet="Sheet", target=f"A{i}", value=i),
            previous_value=None,
        )
    await test_db.commit()

    workbook = await replay.replay_to(test_db, session_id=session_id, up_to_sequence=3)
    pyxl = workbook.raw
    assert pyxl["Sheet"]["A1"].value == 1
    assert pyxl["Sheet"]["A2"].value == 2
    assert pyxl["Sheet"]["A3"].value == 3
    # Actions 4-10 must not have been applied.
    assert pyxl["Sheet"]["A4"].value is None
    assert pyxl["Sheet"]["A10"].value is None


async def test_replay_skips_pending_and_rejected(
    test_db: AsyncSession, session_id: uuid.UUID
) -> None:
    """Only status IN (APPLIED, UNDONE) replay; others never mutated the workbook."""
    await audit.record(
        test_db,
        session_id=session_id,
        agent="executor",
        action=WriteValue(sheet="Sheet", target="A1", value=100),
        previous_value=None,
        status=ActionStatus.APPLIED,
    )
    await audit.record(
        test_db,
        session_id=session_id,
        agent="executor",
        action=WriteValue(sheet="Sheet", target="B1", value=200),
        previous_value=None,
        status=ActionStatus.PENDING_APPROVAL,
    )
    await audit.record(
        test_db,
        session_id=session_id,
        agent="executor",
        action=WriteValue(sheet="Sheet", target="C1", value=300),
        previous_value=None,
        status=ActionStatus.REJECTED,
    )
    await audit.record(
        test_db,
        session_id=session_id,
        agent="executor",
        action=WriteValue(sheet="Sheet", target="D1", value=400),
        previous_value=None,
        status=ActionStatus.UNDONE,
    )
    await test_db.commit()

    workbook = await replay.replay_to(test_db, session_id=session_id)
    pyxl = workbook.raw
    assert pyxl["Sheet"]["A1"].value == 100  # APPLIED → replayed
    assert pyxl["Sheet"]["B1"].value is None  # PENDING_APPROVAL → skipped
    assert pyxl["Sheet"]["C1"].value is None  # REJECTED → skipped
    assert pyxl["Sheet"]["D1"].value == 400  # UNDONE → still replayed


async def test_replay_from_original_upload(
    test_db: AsyncSession, temp_storage: Path
) -> None:
    """If session has an original_workbook_path, replay starts from it."""
    from openpyxl import Workbook as OpenpyxlWorkbook

    from shotcut.db.models import Session as SessionRow

    sid = uuid.uuid4()
    session_dir = temp_storage / "workbooks" / str(sid)
    session_dir.mkdir(parents=True, exist_ok=True)
    original_path = session_dir / "original.xlsx"

    pyxl = OpenpyxlWorkbook()
    pyxl.active.title = "Sheet1"
    pyxl.active["A1"] = "user"
    pyxl.save(original_path)

    test_db.add(
        SessionRow(
            id=sid,
            workbook_path=str(session_dir / "current.xlsx"),
            original_workbook_path=str(original_path),
        )
    )
    await test_db.commit()

    # Agent wrote B1 on top.
    await audit.record(
        test_db,
        session_id=sid,
        agent="executor",
        action=WriteValue(sheet="Sheet1", target="B1", value=42),
        previous_value=None,
    )
    await test_db.commit()

    workbook = await replay.replay_to(test_db, session_id=sid)
    assert workbook.raw["Sheet1"]["A1"].value == "user"
    assert workbook.raw["Sheet1"]["B1"].value == 42


async def test_locate_action_by_client_id(
    test_db: AsyncSession, session_id: uuid.UUID
) -> None:
    """Given a client_action_id, find the DB row in any session/branch."""
    action = WriteFormula(sheet="Sheet", target="A1", formula="=1+1")
    row = await audit.record(
        test_db,
        session_id=session_id,
        agent="executor",
        action=action,
        previous_value=None,
    )
    await test_db.commit()

    located = await replay.locate_action_by_client_id(
        test_db, session_id=session_id, client_action_id=action.client_action_id
    )
    assert located is not None
    assert located.id == row.id

    missing = await replay.locate_action_by_client_id(
        test_db, session_id=session_id, client_action_id=uuid.uuid4()
    )
    assert missing is None


async def test_replay_formulas_preserved_through_reconstruction(
    test_db: AsyncSession, session_id: uuid.UUID
) -> None:
    """Stage 2's formula-preservation invariant holds on replayed workbooks."""
    await audit.record(
        test_db,
        session_id=session_id,
        agent="executor",
        action=WriteValue(sheet="Sheet", target="A1", value=10),
        previous_value=None,
    )
    await audit.record(
        test_db,
        session_id=session_id,
        agent="executor",
        action=WriteValue(sheet="Sheet", target="A2", value=20),
        previous_value=None,
    )
    await audit.record(
        test_db,
        session_id=session_id,
        agent="executor",
        action=WriteFormula(sheet="Sheet", target="A3", formula="=A1+A2"),
        previous_value=None,
    )
    await test_db.commit()

    workbook = await replay.replay_to(test_db, session_id=session_id)
    # Cell holds the formula string, not its evaluated value.
    assert workbook.raw["Sheet"]["A3"].value == "=A1+A2"
    # Engine evaluates correctly through replay.
    assert workbook.evaluate_cell("Sheet", "A3") == 30


async def test_replay_nonexistent_session_raises(
    test_db: AsyncSession,
) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        await replay.replay_to(test_db, session_id=uuid.uuid4())


# Suppress ruff warning on AddSheet usage — referenced below for completeness
_ = AddSheet
