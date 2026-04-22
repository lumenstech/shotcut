from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.db.models import Action
from shotcut.spreadsheet.actions import Action as AgentAction


async def record(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    agent: str,
    action: AgentAction,
    previous_value: dict[str, Any] | None,
    reasoning: str | None = None,
) -> Action:
    next_seq = await _next_sequence(db, session_id)
    row = Action(
        session_id=session_id,
        sequence=next_seq,
        agent=agent,
        action_type=action.type,
        sheet=action.sheet,
        target_range=action.target,
        previous_value=previous_value,
        new_value=action.model_dump(mode="json"),
        reasoning=reasoning,
    )
    db.add(row)
    await db.flush()
    return row


async def _next_sequence(db: AsyncSession, session_id: uuid.UUID) -> int:
    stmt = select(Action.sequence).where(Action.session_id == session_id).order_by(
        Action.sequence.desc()
    ).limit(1)
    latest = (await db.execute(stmt)).scalar_one_or_none()
    return (latest or 0) + 1
