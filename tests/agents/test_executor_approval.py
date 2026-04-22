"""Stage 3 end-to-end acceptance tests.

Exercises the full orchestrator path:
- Upload a workbook with user values at A1:A10
- Agent attempts to write A5 → action is *staged* as pending_approval,
  workbook is NOT mutated
- POST /actions/{id}/approve applies with force_override=True, audit log
  records the override
- POST /actions/{id}/reject sets status=rejected, doesn't touch the workbook

Planner / executor / verifier are monkeypatched to canned outputs —
we're testing the orchestrator's occupancy-blocking logic, not the LLM
agents.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from openpyxl import Workbook as OpenpyxlWorkbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.agents import executor, orchestrator, planner, verifier
from shotcut.agents.planner import Plan, PlanStep
from shotcut.agents.verifier import VerificationReport
from shotcut.db.models import Action, ActionStatus, Session as SessionRow
from shotcut.spreadsheet.actions import Action as AgentAction
from shotcut.spreadsheet.actions import WriteValue


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_user_xlsx(path: Path) -> None:
    """Workbook with user values at A1:A10 — the test scenario from the
    original Stage 3 spec."""
    pyxl = OpenpyxlWorkbook()
    ws = pyxl.active
    ws.title = "Sheet1"
    for i in range(1, 11):
        ws[f"A{i}"] = i * 100
    pyxl.save(path)


@pytest_asyncio.fixture
async def session_with_upload(
    test_db: AsyncSession, temp_storage: Path
) -> tuple[SessionRow, Path]:
    """A session whose original_workbook_path has user content at A1:A10."""
    session_id = uuid.uuid4()
    session_dir = temp_storage / "workbooks" / str(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    original_path = session_dir / "original.xlsx"
    _build_user_xlsx(original_path)

    row = SessionRow(
        id=session_id,
        workbook_path=str(original_path),
        original_workbook_path=str(original_path),
    )
    test_db.add(row)
    await test_db.commit()
    await test_db.refresh(row)
    return row, original_path


def _patch_agents(
    monkeypatch: pytest.MonkeyPatch,
    *,
    actions_to_emit: list[AgentAction],
) -> None:
    """Stub planner / executor / verifier with canned, deterministic output."""

    async def fake_plan(prompt: str, workbook_summary: dict) -> Plan:
        return Plan(
            summary="test plan",
            steps=[PlanStep(title="step 1", description="write actions")],
        )

    async def fake_execute(
        step: PlanStep, workbook_summary: dict, max_iterations: int = 6
    ) -> list[AgentAction]:
        return actions_to_emit

    async def fake_verify(
        workbook: object, *, prompt: str, pending_actions: object = None
    ) -> VerificationReport:
        return VerificationReport(findings=[], confidence=1.0)

    monkeypatch.setattr(planner, "plan", fake_plan)
    monkeypatch.setattr(executor, "execute", fake_execute)
    monkeypatch.setattr(verifier, "verify", fake_verify)


# ---------------------------------------------------------------------------
# Acceptance: user-cell write is staged, not applied
# ---------------------------------------------------------------------------


async def test_orchestrator_stages_pending_approval_for_user_cell(
    test_db: AsyncSession,
    session_with_upload: tuple[SessionRow, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Writing A5 where A1:A10 are user-sourced → action is staged,
    workbook unchanged."""
    session_row, original_path = session_with_upload

    write_a5 = WriteValue(sheet="Sheet1", target="A5", value=9999)
    _patch_agents(monkeypatch, actions_to_emit=[write_a5])

    result = await orchestrator.run(
        test_db,
        session_id=session_row.id,
        prompt="update A5",
        input_path=original_path,
        original_path=original_path,
    )

    # Action count: zero applied (blocked), one pending.
    assert result.actions_applied == 0
    assert len(result.pending_approvals) == 1
    pending = result.pending_approvals[0]
    assert pending.sheet == "Sheet1"
    assert pending.target == "A5"
    assert "Sheet1!A5" in pending.reason

    # Audit row persisted with pending_approval status.
    stmt = select(Action).where(Action.session_id == session_row.id)
    rows = (await test_db.execute(stmt)).scalars().all()
    assert len(rows) == 1
    assert rows[0].status is ActionStatus.PENDING_APPROVAL
    assert rows[0].force_override is False
    assert rows[0].approval_required_reason is not None

    # Workbook on disk still has the user's original value at A5.
    from shotcut.spreadsheet.workbook import Workbook

    reloaded = Workbook.from_xlsx(Path(session_row.workbook_path))
    assert reloaded.evaluate_cell("Sheet1", "A5") == 500


async def test_orchestrator_applies_non_conflicting_action(
    test_db: AsyncSession,
    session_with_upload: tuple[SessionRow, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Writing B1 (empty) alongside user data at A1:A10 → applied, no pending."""
    session_row, original_path = session_with_upload

    write_b1 = WriteValue(sheet="Sheet1", target="B1", value=42)
    _patch_agents(monkeypatch, actions_to_emit=[write_b1])

    result = await orchestrator.run(
        test_db,
        session_id=session_row.id,
        prompt="add B1",
        input_path=original_path,
        original_path=original_path,
    )

    assert result.actions_applied == 1
    assert result.pending_approvals == []

    stmt = select(Action).where(Action.session_id == session_row.id)
    rows = (await test_db.execute(stmt)).scalars().all()
    assert len(rows) == 1
    assert rows[0].status is ActionStatus.APPLIED
    assert rows[0].force_override is False


async def test_orchestrator_without_upload_applies_every_action(
    test_db: AsyncSession,
    temp_storage: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No original upload → empty OccupancyMap → every action applies."""
    session_id = uuid.uuid4()
    session_dir = temp_storage / "workbooks" / str(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    row = SessionRow(
        id=session_id,
        workbook_path=str(session_dir / "current.xlsx"),
        original_workbook_path=None,
    )
    test_db.add(row)
    await test_db.commit()

    _patch_agents(
        monkeypatch,
        actions_to_emit=[
            WriteValue(sheet="Sheet", target="A1", value=1),
            WriteValue(sheet="Sheet", target="A2", value=2),
        ],
    )

    result = await orchestrator.run(
        test_db,
        session_id=session_id,
        prompt="blank start",
        input_path=None,
        original_path=None,
    )
    assert result.actions_applied == 2
    assert result.pending_approvals == []


# ---------------------------------------------------------------------------
# Acceptance: approve applies with force_override=True
# ---------------------------------------------------------------------------


async def test_approve_applies_with_force_override(
    test_db: AsyncSession,
    session_with_upload: tuple[SessionRow, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After staging, approving the action applies it and sets force_override."""
    session_row, original_path = session_with_upload

    write_a5 = WriteValue(sheet="Sheet1", target="A5", value=9999)
    _patch_agents(monkeypatch, actions_to_emit=[write_a5])

    await orchestrator.run(
        test_db,
        session_id=session_row.id,
        prompt="update A5",
        input_path=original_path,
        original_path=original_path,
    )

    pending = (
        await test_db.execute(
            select(Action).where(Action.session_id == session_row.id)
        )
    ).scalar_one()
    assert pending.status is ActionStatus.PENDING_APPROVAL

    apply_result = await orchestrator.apply_pending_action(test_db, action_row=pending)

    # Row updated in place.
    await test_db.refresh(pending)
    assert pending.status is ActionStatus.APPLIED
    assert pending.force_override is True
    assert pending.previous_value is not None
    # Previous value snapshot captured the user's original 500 at A5.
    assert apply_result["previous_value"] is not None

    # Workbook now has the overridden value.
    from shotcut.spreadsheet.workbook import Workbook

    await test_db.refresh(session_row)
    reloaded = Workbook.from_xlsx(Path(session_row.workbook_path))
    assert reloaded.evaluate_cell("Sheet1", "A5") == 9999


async def test_approve_rejects_non_pending(
    test_db: AsyncSession,
    session_with_upload: tuple[SessionRow, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apply_pending_action raises if the action isn't pending_approval."""
    session_row, original_path = session_with_upload
    _patch_agents(
        monkeypatch,
        actions_to_emit=[WriteValue(sheet="Sheet1", target="B1", value=1)],
    )
    await orchestrator.run(
        test_db,
        session_id=session_row.id,
        prompt="add B1",
        input_path=original_path,
        original_path=original_path,
    )
    applied = (
        await test_db.execute(
            select(Action).where(Action.session_id == session_row.id)
        )
    ).scalar_one()
    assert applied.status is ActionStatus.APPLIED

    with pytest.raises(ValueError, match="not pending_approval"):
        await orchestrator.apply_pending_action(test_db, action_row=applied)


# ---------------------------------------------------------------------------
# Acceptance: reject marks rejected, doesn't touch workbook
# ---------------------------------------------------------------------------


async def test_reject_endpoint_contract(
    test_db: AsyncSession,
    session_with_upload: tuple[SessionRow, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulated reject — set status=REJECTED; workbook state unchanged."""
    session_row, original_path = session_with_upload

    write_a5 = WriteValue(sheet="Sheet1", target="A5", value=9999)
    _patch_agents(monkeypatch, actions_to_emit=[write_a5])

    await orchestrator.run(
        test_db,
        session_id=session_row.id,
        prompt="update A5",
        input_path=original_path,
        original_path=original_path,
    )

    pending = (
        await test_db.execute(
            select(Action).where(Action.session_id == session_row.id)
        )
    ).scalar_one()

    # Simulate the endpoint's action: flip status and commit.
    pending.status = ActionStatus.REJECTED
    await test_db.commit()

    # Workbook untouched.
    from shotcut.spreadsheet.workbook import Workbook

    await test_db.refresh(session_row)
    reloaded = Workbook.from_xlsx(Path(session_row.workbook_path))
    assert reloaded.evaluate_cell("Sheet1", "A5") == 500
