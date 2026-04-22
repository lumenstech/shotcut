"""Render a session's action history as a markdown audit report.

Produces a human-readable record suitable for compliance or review.
Not a machine-parseable export — callers that need structured audit
data use the existing `GET /sessions/{id}/audit` JSON endpoint.
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.db.models import Action, ActionStatus, Session as SessionRow


async def render_markdown(
    db: AsyncSession, *, session_id: uuid.UUID
) -> str:
    """Return a markdown string describing every action in the session,
    in sequence order.
    """
    session = await db.get(SessionRow, session_id)
    if session is None:
        raise ValueError(f"session {session_id} does not exist")

    stmt = (
        select(Action)
        .where(Action.session_id == session_id)
        .order_by(Action.sequence)
    )
    rows = list((await db.execute(stmt)).scalars().all())

    buf = io.StringIO()
    buf.write(f"# Audit report — session `{session_id}`\n\n")
    buf.write(f"- **Title:** {session.title or '(none)'}\n")
    buf.write(f"- **Created:** {session.created_at.isoformat()}\n")
    buf.write(f"- **Total actions:** {len(rows)}\n\n")

    if not rows:
        buf.write("_No actions recorded._\n")
        return buf.getvalue()

    counts = _status_counts(rows)
    buf.write("## Summary\n\n")
    for status, count in counts.items():
        buf.write(f"- {status.value}: {count}\n")
    buf.write("\n## Actions\n\n")

    for row in rows:
        _render_row(buf, row)

    return buf.getvalue()


def _status_counts(rows: list[Action]) -> dict[ActionStatus, int]:
    counts: dict[ActionStatus, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return counts


def _render_row(buf: io.StringIO, row: Action) -> None:
    heading = f"### #{row.sequence} — `{row.action_type}`"
    if row.sheet and row.target_range:
        heading += f" @ `{row.sheet}!{row.target_range}`"
    buf.write(heading + "\n\n")

    buf.write(f"- **Status:** `{row.status.value}`")
    if row.force_override:
        buf.write(" (force override)")
    buf.write("\n")
    buf.write(f"- **Agent:** {row.agent}\n")
    buf.write(f"- **Action id:** `{row.id}`\n")
    buf.write(f"- **Client action id:** `{row.client_action_id}`\n")
    if row.parent_action_id:
        buf.write(f"- **Parent action:** `{row.parent_action_id}`\n")
    buf.write(f"- **Timestamp:** {_fmt_ts(row.created_at)}\n")
    if row.approval_required_reason:
        buf.write(f"- **Approval reason:** {row.approval_required_reason}\n")
    if row.reasoning:
        # Reasoning can be multi-line (e.g. verifier findings appended).
        # Indent so markdown preserves line breaks within the bullet.
        indented = row.reasoning.replace("\n", "\n  ")
        buf.write(f"- **Reasoning:**\n  {indented}\n")
    if row.previous_value is not None:
        buf.write(f"- **Before:** `{row.previous_value}`\n")
    if row.new_value is not None:
        buf.write(f"- **After:** `{row.new_value}`\n")
    buf.write("\n")


def _fmt_ts(ts: datetime | None) -> str:
    return ts.isoformat() if ts else "(unset)"
