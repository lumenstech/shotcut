"""Reconstruct workbook state at an arbitrary point in a session's history.

For a session with N actions, replay walks rows in sequence order and
applies the ones that actually mutated the workbook (status in
{APPLIED, UNDONE}). REJECTED and PENDING_APPROVAL rows never touched
the workbook and are skipped.

UNDONE rows are replayed because they *were* applied to the workbook
during the session's lifetime — their effect was later reversed by a
subsequent inverse action (also present in the action stream). Replaying
both gives the net post-undo state, matching what the live session
would observe.

Starting state:
- Original upload (session.original_workbook_path) if present.
- Blank workbook otherwise.

Scope: no snapshot optimization in this MVP. Walk-from-origin is O(N)
openpyxl `apply()` calls — milliseconds per cell at MVP workbook sizes.
When sessions grow to thousands of actions, add a `workbook_snapshots`
table that caches state every K actions; `replay_to` then finds the
nearest snapshot ≤ target and walks forward from there. The public API
(`replay_to` returning a `Workbook`) stays stable across the upgrade.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.db import audit
from shotcut.db.models import Action, ActionStatus
from shotcut.db.models import Session as SessionRow
from shotcut.spreadsheet.workbook import Workbook


# Action statuses that actually touched the workbook during the live
# session and must therefore be replayed.
_REPLAYABLE = (ActionStatus.APPLIED, ActionStatus.UNDONE)


async def replay_to(
    db: AsyncSession, *, session_id: uuid.UUID, up_to_sequence: int | None = None
) -> Workbook:
    """Return a fresh Workbook reconstructed up to (and including) sequence N.

    `up_to_sequence=None` replays the entire history. The returned
    workbook is an in-memory clone; the persisted `session.workbook_path`
    is not touched.
    """
    session_row = await db.get(SessionRow, session_id)
    if session_row is None:
        raise ValueError(f"session {session_id} does not exist")

    workbook = _load_starting_state(session_row)

    stmt = (
        select(Action)
        .where(Action.session_id == session_id)
        .where(Action.status.in_(_REPLAYABLE))
        .order_by(Action.sequence)
    )
    if up_to_sequence is not None:
        stmt = stmt.where(Action.sequence <= up_to_sequence)

    rows = (await db.execute(stmt)).scalars().all()
    for row in rows:
        action = audit.row_to_action(row)
        workbook.apply(action)

    return workbook


def _load_starting_state(session_row: SessionRow) -> Workbook:
    """Start from the uploaded original if present; blank otherwise."""
    if session_row.original_workbook_path:
        original_path = Path(session_row.original_workbook_path)
        if original_path.exists():
            return Workbook.from_xlsx(original_path)
    return Workbook.blank()


async def locate_action_by_client_id(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    client_action_id: uuid.UUID,
) -> Action | None:
    """Find the row in `session_id` with matching `client_action_id`, if any.

    Stage 5 branching copies actions with the same `client_action_id`
    into a child session. Given a logical action, callers can locate its
    row in any branch it exists in.
    """
    stmt = (
        select(Action)
        .where(Action.session_id == session_id)
        .where(Action.client_action_id == client_action_id)
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()
