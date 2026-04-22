"""Durable state-machine checkpointing.

Thin helpers over the `orchestrator_states` table. The durable
orchestrator calls `create()` on start, `save()` after each phase
transition, and `load()` on resume. Plans serialize via Pydantic's
`model_dump()`; on load they rehydrate via `Plan.model_validate()`.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.agents.planner import Plan
from shotcut.db.models import OrchestratorState, OrchestratorStateEnum


TERMINAL = {OrchestratorStateEnum.DONE, OrchestratorStateEnum.FAILED}


async def create(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    prompt: str,
) -> OrchestratorState:
    """Insert the initial orchestrator state row (state=PLANNING).

    Raises if a row already exists for `session_id` — callers should
    `load()` first and decide whether to resume or reject.
    """
    row = OrchestratorState(
        session_id=session_id,
        state=OrchestratorStateEnum.PLANNING,
        prompt=prompt,
        plan=None,
        current_step_index=0,
    )
    db.add(row)
    await db.flush()
    return row


async def load(
    db: AsyncSession, *, session_id: uuid.UUID
) -> OrchestratorState | None:
    """Return the row for `session_id`, if any."""
    return await db.get(OrchestratorState, session_id)


async def save(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    state: OrchestratorStateEnum | None = None,
    plan: Plan | None = None,
    current_step_index: int | None = None,
    error: str | None = None,
) -> OrchestratorState:
    """Update the row in place. Only the provided fields are changed.

    The caller commits (we keep this narrow so it composes into the
    larger phase-transition transactions the durable orchestrator
    drives).
    """
    row = await db.get(OrchestratorState, session_id)
    if row is None:
        raise ValueError(f"no orchestrator_states row for session {session_id}")
    if state is not None:
        row.state = state
    if plan is not None:
        row.plan = plan.model_dump(mode="json")
    if current_step_index is not None:
        row.current_step_index = current_step_index
    if error is not None:
        row.error = error
    return row


async def find_non_terminal(db: AsyncSession) -> list[OrchestratorState]:
    """Rows whose state is not DONE/FAILED — used by the startup resume scan."""
    stmt = select(OrchestratorState).where(
        OrchestratorState.state.not_in([s.value for s in TERMINAL])
    )
    return list((await db.execute(stmt)).scalars().all())


def plan_from_row(row: OrchestratorState) -> Plan | None:
    """Rehydrate a Plan from the row's JSON blob. None if no plan yet."""
    blob: dict[str, Any] | None = row.plan
    if blob is None:
        return None
    return Plan.model_validate(blob)
