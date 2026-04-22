"""Async orchestrator that wires planner → executor → verifier together.

Builds an OccupancyMap from `session.original_workbook_path` and checks
every executor-emitted Action against it. Actions that would overwrite
user-sourced cells are persisted with `status=pending_approval` and
`approval_required_reason` populated — the workbook is NOT mutated.

The Stage 4 verifier then evaluates a CLONE of the current workbook
with pending actions applied, so the user sees findings the applied
state *would* produce. Critical findings whose target cell falls inside
a pending action's range are appended to that action's `reasoning`
column; `status` stays `pending_approval` (verifier never auto-rejects).
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
from shotcut.spreadsheet.actions import Action as AgentAction
from shotcut.spreadsheet.occupancy import OccupancyMap
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
    # (domain_action, db_row) pairs for pending actions from this turn.
    # Kept alongside `pending` so the verifier can run on a clone with
    # these applied and we can attribute findings back to their rows.
    pending_pairs: list[tuple[AgentAction, Action]] = []

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
                pending_pairs.append((action, row))
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

    pending_domain = [dom for dom, _ in pending_pairs]
    # Key attribution on `client_action_id` (domain-owned, stable across
    # serialization and branches) rather than Python's id(). See Stage 5
    # erratum in docs/decisions/0002-schema-consolidation.md.
    row_id_by_client_id = {dom.client_action_id: row.id for dom, row in pending_pairs}
    rows_by_id = {row.id: row for _, row in pending_pairs}

    log.info("verifier: running 5-level pipeline (pending=%d)", len(pending_domain))
    report = await verifier.verify(
        workbook, prompt=prompt, pending_actions=pending_domain
    )
    log.info(
        "verifier: %d findings (%d critical, confidence=%.2f)",
        len(report.findings),
        len(report.critical),
        report.confidence,
    )

    # Attribute critical findings back to the pending actions whose cells
    # they target, and append to the action's reasoning. Status does NOT
    # change — verifier is informational for pending actions, not a gate.
    attributions = verifier.attribute_to_actions(
        report.critical, pending_domain, row_id_by_client_id
    )
    for action_id, findings in attributions.items():
        # Different name from the `row` used earlier in the turn's audit
        # loop so mypy can narrow Optional[Action] → Action cleanly.
        attrib_row = rows_by_id.get(action_id)
        if attrib_row is None:
            continue
        addendum = "\n".join(
            f"[verifier {f.level.value}/{f.severity.value}] {f.message}"
            for f in findings
        )
        attrib_row.reasoning = (attrib_row.reasoning or "") + "\n" + addendum
    if attributions:
        await db.commit()

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
