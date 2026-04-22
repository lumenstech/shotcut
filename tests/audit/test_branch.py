"""Branching: fork a session at an arbitrary sequence.

Stage 5 acceptance:
- Branch at action 50 → child has actions 1-50 copied with matching
  client_action_id and new DB row ids; parent retains original rows.
- Both branches independently navigable after the fork (actions added
  to one branch don't leak to the other).
- SessionBranch row records parent/child/branched_at_sequence.
- Original and branched session contain rows with MATCHING
  client_action_id for copied actions (the point of the Stage 5 erratum).
- Replay across branches: given `client_action_id`, locate the row in
  the child.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.audit import branch, replay
from shotcut.db import audit
from shotcut.db.models import Action, Session as SessionRow, SessionBranch
from shotcut.spreadsheet.actions import WriteValue


@pytest_asyncio.fixture
async def parent_with_history(
    test_db: AsyncSession, temp_storage: Path
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Seed a parent session with 10 applied actions. Returns
    (session_id, client_action_ids in order)."""
    sid = uuid.uuid4()
    session_dir = temp_storage / "workbooks" / str(sid)
    session_dir.mkdir(parents=True, exist_ok=True)

    test_db.add(
        SessionRow(
            id=sid,
            workbook_path=str(session_dir / "current.xlsx"),
            original_workbook_path=None,
        )
    )
    await test_db.commit()

    cids: list[uuid.UUID] = []
    for i in range(1, 11):
        action = WriteValue(sheet="Sheet", target=f"A{i}", value=i * 10)
        await audit.record(
            test_db,
            session_id=sid,
            agent="executor",
            action=action,
            previous_value=None,
        )
        cids.append(action.client_action_id)
    await test_db.commit()
    return sid, cids


async def test_branch_copies_actions_with_matching_client_action_id(
    test_db: AsyncSession,
    parent_with_history: tuple[uuid.UUID, list[uuid.UUID]],
    temp_storage: Path,
) -> None:
    parent_id, parent_cids = parent_with_history

    child = await branch.fork_at(
        test_db, parent_session_id=parent_id, at_sequence=5, title="try alt"
    )

    assert child.id != parent_id
    assert child.title == "try alt"

    # Child has exactly 5 copied actions.
    stmt = (
        select(Action)
        .where(Action.session_id == child.id)
        .order_by(Action.sequence)
    )
    child_rows = list((await test_db.execute(stmt)).scalars().all())
    assert len(child_rows) == 5

    # Each child row has a DIFFERENT DB id but the SAME client_action_id
    # as its parent source. This is the Stage 5 erratum payoff.
    parent_stmt = (
        select(Action)
        .where(Action.session_id == parent_id)
        .where(Action.sequence <= 5)
        .order_by(Action.sequence)
    )
    parent_rows = list((await test_db.execute(parent_stmt)).scalars().all())
    for parent_row, child_row in zip(parent_rows, child_rows):
        assert parent_row.client_action_id == child_row.client_action_id
        assert parent_row.id != child_row.id
        assert child_row.session_id == child.id

    # Child sequence restarts from 1.
    assert [r.sequence for r in child_rows] == [1, 2, 3, 4, 5]


async def test_branch_creates_lineage_row(
    test_db: AsyncSession, parent_with_history: tuple[uuid.UUID, list[uuid.UUID]]
) -> None:
    parent_id, _ = parent_with_history
    child = await branch.fork_at(
        test_db, parent_session_id=parent_id, at_sequence=5
    )

    stmt = select(SessionBranch).where(SessionBranch.child_session_id == child.id)
    lineage_row = (await test_db.execute(stmt)).scalar_one()
    assert lineage_row.parent_session_id == parent_id
    assert lineage_row.branched_at_sequence == 5

    # Helper functions round-trip.
    lineage = await branch.lineage(test_db, session_id=child.id)
    assert len(lineage) == 1
    assert lineage[0].parent_session_id == parent_id

    children = await branch.children(test_db, session_id=parent_id)
    assert len(children) == 1
    assert children[0].child_session_id == child.id


async def test_branches_diverge_independently(
    test_db: AsyncSession,
    parent_with_history: tuple[uuid.UUID, list[uuid.UUID]],
) -> None:
    parent_id, _ = parent_with_history
    child = await branch.fork_at(
        test_db, parent_session_id=parent_id, at_sequence=5
    )

    # Append one action to each branch independently.
    parent_only = WriteValue(sheet="Sheet", target="Z1", value="parent")
    child_only = WriteValue(sheet="Sheet", target="Z1", value="child")
    await audit.record(
        test_db, session_id=parent_id, agent="executor",
        action=parent_only, previous_value=None,
    )
    await audit.record(
        test_db, session_id=child.id, agent="executor",
        action=child_only, previous_value=None,
    )
    await test_db.commit()

    parent_wb = await replay.replay_to(test_db, session_id=parent_id)
    child_wb = await replay.replay_to(test_db, session_id=child.id)

    assert parent_wb.raw["Sheet"]["Z1"].value == "parent"
    assert child_wb.raw["Sheet"]["Z1"].value == "child"


async def test_child_workbook_matches_parent_state_at_branch_point(
    test_db: AsyncSession,
    parent_with_history: tuple[uuid.UUID, list[uuid.UUID]],
) -> None:
    """After forking at sequence 5, the child's current.xlsx contains
    exactly what replay-to-5 produces on the parent."""
    parent_id, _ = parent_with_history
    parent_replayed_to_5 = await replay.replay_to(
        test_db, session_id=parent_id, up_to_sequence=5
    )

    child = await branch.fork_at(
        test_db, parent_session_id=parent_id, at_sequence=5
    )
    from shotcut.spreadsheet.workbook import Workbook
    child_workbook = Workbook.from_xlsx(Path(child.workbook_path))

    parent_cells = {
        (s["name"], c["ref"]): c["value"]
        for s in parent_replayed_to_5.summary(max_cells_per_sheet=10_000)["sheets"]
        for c in s["cells"]
    }
    child_cells = {
        (s["name"], c["ref"]): c["value"]
        for s in child_workbook.summary(max_cells_per_sheet=10_000)["sheets"]
        for c in s["cells"]
    }
    assert parent_cells == child_cells


async def test_locate_client_action_id_across_branches(
    test_db: AsyncSession,
    parent_with_history: tuple[uuid.UUID, list[uuid.UUID]],
) -> None:
    """Replay-across-branches acceptance: given a client_action_id,
    locate the row in either branch."""
    parent_id, parent_cids = parent_with_history
    child = await branch.fork_at(
        test_db, parent_session_id=parent_id, at_sequence=5
    )

    # The client_action_id at parent sequence 3 exists in both sessions.
    target_cid = parent_cids[2]  # sequence 3

    found_in_parent = await replay.locate_action_by_client_id(
        test_db, session_id=parent_id, client_action_id=target_cid
    )
    found_in_child = await replay.locate_action_by_client_id(
        test_db, session_id=child.id, client_action_id=target_cid
    )
    assert found_in_parent is not None
    assert found_in_child is not None
    assert found_in_parent.client_action_id == found_in_child.client_action_id
    # Different DB row ids across branches.
    assert found_in_parent.id != found_in_child.id

    # A client_action_id from sequence 8 only exists in the parent.
    post_branch_cid = parent_cids[7]  # sequence 8
    assert await replay.locate_action_by_client_id(
        test_db, session_id=parent_id, client_action_id=post_branch_cid
    ) is not None
    assert await replay.locate_action_by_client_id(
        test_db, session_id=child.id, client_action_id=post_branch_cid
    ) is None
