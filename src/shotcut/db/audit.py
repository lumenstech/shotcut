from __future__ import annotations

import uuid
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.db.models import Action, ActionStatus
from shotcut.spreadsheet.actions import Action as AgentAction

# Pydantic adapter for the discriminated-union of agent actions. Used to
# reconstruct a typed Action from the `new_value` JSON blob when Stage 3
# approves a previously-staged pending action.
_action_adapter: TypeAdapter[AgentAction] = TypeAdapter(AgentAction)


async def record(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    agent: str,
    action: AgentAction,
    previous_value: dict[str, Any] | None,
    reasoning: str | None = None,
    status: ActionStatus = ActionStatus.APPLIED,
    approval_required_reason: str | None = None,
    force_override: bool = False,
    tenant_id: uuid.UUID | None = None,
    user_sub: str | None = None,
) -> Action:
    """Insert one audit row.

    `tenant_id` and `user_sub` are populated from Stage 8's auth
    middleware. They default to None so Stage 1–7 call sites that
    predate auth continue to work (their rows land as "pre-auth" data;
    production's RLS policies include an escape-hatch for empty
    tenant contexts — see alembic/versions/0006_rls_policies.py).
    """
    next_seq = await _next_sequence(db, session_id)
    row = Action(
        session_id=session_id,
        sequence=next_seq,
        # Persist the domain Action's client_action_id so it survives
        # reconstruction (row_to_action) and branching (Stage 5).
        client_action_id=action.client_action_id,
        agent=agent,
        action_type=action.type,
        sheet=action.sheet,
        target_range=action.target,
        previous_value=previous_value,
        new_value=action.model_dump(mode="json"),
        reasoning=reasoning,
        status=status,
        approval_required_reason=approval_required_reason,
        force_override=force_override,
        tenant_id=tenant_id,
        user_sub=user_sub,
    )
    db.add(row)
    await db.flush()
    return row


def row_to_action(row: Action) -> AgentAction:
    """Reconstruct a typed Action object from a persisted audit row.

    Used by the approve endpoint to re-apply a previously-staged action.
    """
    if row.new_value is None:
        raise ValueError(f"action {row.id} has no new_value payload")
    return _action_adapter.validate_python(row.new_value)


async def _next_sequence(db: AsyncSession, session_id: uuid.UUID) -> int:
    stmt = (
        select(Action.sequence)
        .where(Action.session_id == session_id)
        .order_by(Action.sequence.desc())
        .limit(1)
    )
    latest = (await db.execute(stmt)).scalar_one_or_none()
    return (latest or 0) + 1
