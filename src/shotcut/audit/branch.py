"""Fork a session at an arbitrary sequence boundary.

Branching produces a new session whose initial state is the parent's
state at `branched_at_sequence`. Semantics:

- A new Session row is created, with its own workbook_path pointing at
  a fresh per-branch storage directory. The parent session is not
  modified.
- Actions up to and including `branched_at_sequence` in the parent are
  copied into the child. Each copied row gets a new DB id but preserves
  `client_action_id` — so the same logical action is locatable in both
  branches via `replay.locate_action_by_client_id`. Sequence numbers in
  the child restart from 1 to match the child's action count.
- `parent_action_id` is remapped to the child's new row ids so the
  within-branch chain stays intact after copy.
- A `SessionBranch` row records the lineage.
- The parent's workbook state at that sequence (replayed) is saved as
  the child's `current.xlsx`. If the parent has an `original.xlsx`, it's
  copied verbatim so the child has the same user-baseline for occupancy.

After branching, the two sessions evolve independently. Each has its
own `session.workbook_path`, its own audit trail, and its own OccupancyMap.
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.audit import replay
from shotcut.db.models import Action, Session as SessionRow, SessionBranch
from shotcut.storage import get_storage


async def fork_at(
    db: AsyncSession,
    *,
    parent_session_id: uuid.UUID,
    at_sequence: int,
    title: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> SessionRow:
    """Create a child session branched from `parent_session_id` at
    sequence `at_sequence`.

    `tenant_id` is Stage 8's addition: copied from the calling user's
    context onto the child session + lineage row so RLS policies scope
    the child to the same tenant as the parent. Defaults to the parent's
    tenant_id when None (preserves the Stage 5 call-site contract for
    tests that don't plumb auth).

    Returns the child SessionRow (refreshed from DB).
    """
    parent = await db.get(SessionRow, parent_session_id)
    if parent is None:
        raise ValueError(f"parent session {parent_session_id} does not exist")
    effective_tenant = tenant_id if tenant_id is not None else parent.tenant_id

    child_id = uuid.uuid4()
    storage = get_storage()

    # 1. Reconstruct parent's workbook at the branch point.
    branched_workbook = await replay.replay_to(
        db, session_id=parent_session_id, up_to_sequence=at_sequence
    )
    child_current_path = storage.local_path(child_id, "current.xlsx")
    assert child_current_path is not None, "local storage required"
    branched_workbook.save(child_current_path)

    # 2. Copy the parent's original.xlsx if it exists, so the child has
    #    the same user-baseline for occupancy.
    child_original_path: Path | None = None
    if parent.original_workbook_path:
        src = Path(parent.original_workbook_path)
        if src.exists():
            dst = storage.local_path(child_id, "original.xlsx")
            assert dst is not None
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            child_original_path = dst

    # 3. Create the child SessionRow.
    child = SessionRow(
        id=child_id,
        title=title or f"Branched from {parent_session_id} at seq {at_sequence}",
        workbook_path=str(child_current_path),
        original_workbook_path=(
            str(child_original_path) if child_original_path else None
        ),
        tenant_id=effective_tenant,
    )
    db.add(child)
    await db.flush()

    # 4. Copy actions ≤ at_sequence into the child with new DB ids,
    #    resequenced from 1, parent_action_id remapped.
    stmt = (
        select(Action)
        .where(Action.session_id == parent_session_id)
        .where(Action.sequence <= at_sequence)
        .order_by(Action.sequence)
    )
    parent_rows = (await db.execute(stmt)).scalars().all()

    id_remap: dict[uuid.UUID, uuid.UUID] = {}
    for new_seq, source_row in enumerate(parent_rows, start=1):
        new_id = uuid.uuid4()
        id_remap[source_row.id] = new_id
        remapped_parent = (
            id_remap.get(source_row.parent_action_id)
            if source_row.parent_action_id
            else None
        )
        db.add(
            Action(
                id=new_id,
                session_id=child_id,
                sequence=new_seq,
                client_action_id=source_row.client_action_id,  # identity across branches
                agent=source_row.agent,
                action_type=source_row.action_type,
                sheet=source_row.sheet,
                target_range=source_row.target_range,
                previous_value=source_row.previous_value,
                new_value=source_row.new_value,
                reasoning=source_row.reasoning,
                status=source_row.status,
                approval_required_reason=source_row.approval_required_reason,
                force_override=source_row.force_override,
                parent_action_id=remapped_parent,
                tenant_id=source_row.tenant_id,
                user_sub=source_row.user_sub,
            )
        )

    # 5. Record the lineage edge.
    db.add(
        SessionBranch(
            child_session_id=child_id,
            parent_session_id=parent_session_id,
            branched_at_sequence=at_sequence,
            tenant_id=effective_tenant,
        )
    )

    await db.commit()
    await db.refresh(child)
    return child


async def lineage(db: AsyncSession, *, session_id: uuid.UUID) -> list[SessionBranch]:
    """Return lineage edges where `session_id` is the child.

    For the MVP we walk one step at a time; deep ancestry chains can
    follow the chain by repeated calls. Returns an empty list for root
    sessions (never branched from anything).
    """
    stmt = select(SessionBranch).where(SessionBranch.child_session_id == session_id)
    return list((await db.execute(stmt)).scalars().all())


async def children(db: AsyncSession, *, session_id: uuid.UUID) -> list[SessionBranch]:
    """Return all branches that were forked from `session_id`."""
    stmt = select(SessionBranch).where(SessionBranch.parent_session_id == session_id)
    return list((await db.execute(stmt)).scalars().all())


