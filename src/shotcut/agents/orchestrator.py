"""Async orchestrator that wires planner → executor → verifier together.

Builds an OccupancyMap from `session.original_workbook_path` and checks
every executor-emitted Action against it. If an action would overwrite a
user-sourced cell, it's persisted with `status=pending_approval` and
`approval_required_reason` populated — the workbook is NOT mutated. The
caller (approve endpoint) applies those actions explicitly.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.agents import executor, planner, verifier
from shotcut.agents.planner import Plan
from shotcut.agents.verifier import VerificationReport
from shotcut.db import audit
from shotcut.db.models import Action, ActionStatus
from shotcut.db.models import Session as SessionRow
from shotcut.spreadsheet.occupancy import OccupancyMap
from shotcut.spreadsheet.validator import ValidationIssue
from shotcut.spreadsheet.workbook import Workbook
from shotcut.storage import get_storage

log = logging.getLogger(__name__)


@dataclass
class PendingApproval:
    action_id: uuid.UUID
    sheet: str | None
    target: str | None
    action_type: str
    reason: str


@dataclass
class RunResult:
    plan: Plan
    actions_applied: int
    pending_approvals: list[PendingApproval]
    syntactic_issues: list[ValidationIssue]
    verification: VerificationReport
    output_path: Path


async def run(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    prompt: str,
    input_path: Path | None,
    original_path: Path | None,
) -> RunResult:
    """Run one turn of planner → executor → verifier.

    `input_path` is the current workbook state (what the agent sees when
    planning). `original_path` is the immutable upload (what we use to
    derive occupancy). Both may be None on a fresh blank session — in
    that case OccupancyMap is empty and no approvals are needed.
    """
    workbook = Workbook.from_xlsx(input_path) if input_path else Workbook.blank()
    occupancy = (
        OccupancyMap.from_original(Workbook.from_xlsx(original_path))
        if original_path is not None and original_path.exists()
        else OccupancyMap.empty()
    )
    log.info(
        "orchestrator: starting session=%s user_cells=%d",
        session_id,
        occupancy.user_cell_count,
    )

    plan = await planner.plan(prompt, workbook.summary())
    log.info("planner: %d steps", len(plan.steps))

    applied = 0
    pending: list[PendingApproval] = []

    for step in plan.steps:
        log.info("executor: step '%s'", step.title)
        actions = await executor.execute(step, workbook.summary())
        for action in actions:
            check = occupancy.check(action)
            if check.blocked:
                row = await audit.record(
                    db,
                    session_id=session_id,
                    agent="executor",
                    action=action,
                    previous_value=None,
                    reasoning=step.title,
                    status=ActionStatus.PENDING_APPROVAL,
                    approval_required_reason=check.reason,
                    force_override=False,
                )
                pending.append(
                    PendingApproval(
                        action_id=row.id,
                        sheet=row.sheet,
                        target=row.target_range,
                        action_type=row.action_type,
                        reason=check.reason or "overwrite",
                    )
                )
                log.info("staged pending approval: %s", check.reason)
                continue

            previous = workbook.apply(action)
            await audit.record(
                db,
                session_id=session_id,
                agent="executor",
                action=action,
                previous_value=previous,
                reasoning=step.title,
                status=ActionStatus.APPLIED,
            )
            applied += 1
        await db.commit()

    log.info("verifier: syntactic pass")
    syntactic = verifier.syntactic_issues(workbook)
    log.info("verifier: %d syntactic issues", len(syntactic))

    log.info("verifier: semantic pass")
    report = await verifier.verify_semantics(prompt, workbook)
    log.info(
        "verifier: %d semantic issues (confidence=%.2f)",
        len(report.issues),
        report.confidence,
    )

    storage = get_storage()
    current_path = storage.local_path(session_id, "current.xlsx")
    assert current_path is not None, "local storage required for save()"
    workbook.save(current_path)

    session_row = await db.get(SessionRow, session_id)
    if session_row is not None:
        session_row.workbook_path = str(current_path)
        await db.commit()

    return RunResult(
        plan=plan,
        actions_applied=applied,
        pending_approvals=pending,
        syntactic_issues=syntactic,
        verification=report,
        output_path=current_path,
    )


async def apply_pending_action(
    db: AsyncSession, *, action_row: Action
) -> dict[str, object]:
    """Apply a previously-staged pending action with force_override=True.

    Reloads the current workbook, applies the action, saves back, updates
    the audit row to status=applied. Returns the previous-value snapshot
    for inclusion in the API response.
    """
    if action_row.status is not ActionStatus.PENDING_APPROVAL:
        raise ValueError(
            f"action {action_row.id} is {action_row.status.value}, "
            "not pending_approval"
        )
    session_row = await db.get(SessionRow, action_row.session_id)
    if session_row is None:
        raise ValueError(f"session {action_row.session_id} no longer exists")

    workbook_path = Path(session_row.workbook_path)
    workbook = (
        Workbook.from_xlsx(workbook_path) if workbook_path.exists() else Workbook.blank()
    )

    action = audit.row_to_action(action_row)
    previous = workbook.apply(action)

    storage = get_storage()
    current_path = storage.local_path(session_row.id, "current.xlsx")
    assert current_path is not None
    workbook.save(current_path)

    action_row.status = ActionStatus.APPLIED
    action_row.force_override = True
    action_row.previous_value = previous
    session_row.workbook_path = str(current_path)
    await db.commit()

    return {"previous_value": previous}
