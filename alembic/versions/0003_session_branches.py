"""stage 5: session_branches lineage table.

Branching forks a session at a sequence boundary. Rather than adding a
`parent_session_id` column to `sessions` (which the Stage 3 erratum
clause prohibits after Stage 3 ship — see
docs/decisions/0002-schema-consolidation.md), lineage lives in its own
join table. Stage 8's RLS policies can attach to `session_branches`
independently without touching the `sessions` schema.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "session_branches",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "child_session_id",
            sa.Uuid,
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "parent_session_id",
            sa.Uuid,
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("branched_at_sequence", sa.Integer, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_session_branches_parent_session_id",
        "session_branches",
        ["parent_session_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_branches_parent_session_id",
        table_name="session_branches",
    )
    op.drop_table("session_branches")
