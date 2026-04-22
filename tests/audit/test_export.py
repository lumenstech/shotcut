"""Audit export — markdown renderer."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.audit import export
from shotcut.db import audit
from shotcut.db.models import ActionStatus, Session as SessionRow
from shotcut.spreadsheet.actions import WriteFormula, WriteValue


@pytest_asyncio.fixture
async def session_id(test_db: AsyncSession, temp_storage: Path) -> uuid.UUID:
    sid = uuid.uuid4()
    test_db.add(
        SessionRow(
            id=sid,
            title="example session",
            workbook_path=str(temp_storage / "workbooks" / str(sid) / "current.xlsx"),
        )
    )
    await test_db.commit()
    return sid


async def test_export_empty_session_renders(
    test_db: AsyncSession, session_id: uuid.UUID
) -> None:
    md = await export.render_markdown(test_db, session_id=session_id)
    assert md.startswith("# Audit report")
    assert "No actions recorded" in md


async def test_export_includes_action_metadata(
    test_db: AsyncSession, session_id: uuid.UUID
) -> None:
    action = WriteValue(sheet="Sheet", target="A1", value=42)
    await audit.record(
        test_db, session_id=session_id, agent="executor",
        action=action, previous_value=None, reasoning="seed value",
    )
    formula = WriteFormula(sheet="Sheet", target="B1", formula="=A1*2")
    await audit.record(
        test_db, session_id=session_id, agent="executor",
        action=formula, previous_value=None, reasoning="doubled",
    )
    await test_db.commit()

    md = await export.render_markdown(test_db, session_id=session_id)

    # Each action gets a heading.
    assert "### #1" in md
    assert "### #2" in md
    # Action types and targets surface.
    assert "write_value" in md
    assert "write_formula" in md
    assert "Sheet!A1" in md
    assert "Sheet!B1" in md
    # Reasoning preserved.
    assert "seed value" in md
    assert "doubled" in md
    # client_action_id fields rendered (stable identity audit requirement).
    assert str(action.client_action_id) in md
    assert str(formula.client_action_id) in md


async def test_export_status_counts(
    test_db: AsyncSession, session_id: uuid.UUID
) -> None:
    """Summary section counts statuses."""
    await audit.record(
        test_db, session_id=session_id, agent="executor",
        action=WriteValue(sheet="Sheet", target="A1", value=1),
        previous_value=None, status=ActionStatus.APPLIED,
    )
    await audit.record(
        test_db, session_id=session_id, agent="executor",
        action=WriteValue(sheet="Sheet", target="A2", value=2),
        previous_value=None, status=ActionStatus.PENDING_APPROVAL,
    )
    await audit.record(
        test_db, session_id=session_id, agent="executor",
        action=WriteValue(sheet="Sheet", target="A3", value=3),
        previous_value=None, status=ActionStatus.REJECTED,
    )
    await test_db.commit()

    md = await export.render_markdown(test_db, session_id=session_id)
    assert "applied: 1" in md
    assert "pending_approval: 1" in md
    assert "rejected: 1" in md
    assert "Total actions:** 3" in md


async def test_export_nonexistent_session_raises(test_db: AsyncSession) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        await export.render_markdown(test_db, session_id=uuid.uuid4())
