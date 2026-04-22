"""stage 3: consolidated schema for stages 3, 5, and 8.

Single migration landing every column those stages need. Nullable
columns for stages 5/8 stay unused until those stages activate them.
Rationale in docs/decisions/0002-schema-consolidation.md.

Types are dialect-portable (sa.Uuid, sa.Enum with default native_enum)
so `alembic upgrade head` runs on Postgres in production and on SQLite
for tests.

Revision ID: 0001
Revises:
Create Date: 2026-04-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACTION_STATUS_VALUES = ("applied", "pending_approval", "rejected", "undone")
ACTION_STATUS_NAME = "action_status"


def upgrade() -> None:
    # --- sessions ------------------------------------------------------
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("title", sa.String(256), nullable=True),
        sa.Column("workbook_path", sa.String(512), nullable=False),
        sa.Column("original_workbook_path", sa.String(512), nullable=True),
        # Populated in Stage 8; nullable no-op until then.
        sa.Column("tenant_id", sa.Uuid, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_sessions_original_workbook_path",
        "sessions",
        ["original_workbook_path"],
    )
    op.create_index("ix_sessions_tenant_id", "sessions", ["tenant_id"])

    # --- actions -------------------------------------------------------
    # sa.Enum handles dialect differences: native ENUM type on Postgres,
    # VARCHAR + CHECK constraint on SQLite. create_type defaults to True
    # on PG (auto-creates on first table use); no-op elsewhere.
    action_status = sa.Enum(
        *ACTION_STATUS_VALUES,
        name=ACTION_STATUS_NAME,
    )

    op.create_table(
        "actions",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "session_id",
            sa.Uuid,
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("agent", sa.String(64), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("sheet", sa.String(255), nullable=True),
        sa.Column("target_range", sa.String(255), nullable=True),
        sa.Column("previous_value", sa.JSON, nullable=True),
        sa.Column("new_value", sa.JSON, nullable=True),
        sa.Column("reasoning", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Stage 3 — approval flow.
        sa.Column(
            "status",
            action_status,
            nullable=False,
            server_default=sa.text("'applied'"),
        ),
        sa.Column("approval_required_reason", sa.Text, nullable=True),
        sa.Column(
            "force_override",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        # Stage 5 — branching (nullable; populated when sessions are forked).
        sa.Column(
            "parent_action_id",
            sa.Uuid,
            sa.ForeignKey("actions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Stage 8 — tenant isolation + audit attribution. Nullable no-ops
        # until Stage 8 populates them from JWT claims.
        sa.Column("tenant_id", sa.Uuid, nullable=True),
        sa.Column("user_sub", sa.String(256), nullable=True),
    )
    op.create_index("ix_actions_session_id", "actions", ["session_id"])
    op.create_index("ix_actions_parent_action_id", "actions", ["parent_action_id"])
    op.create_index("ix_actions_status", "actions", ["status"])
    op.create_index("ix_actions_tenant_id", "actions", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_actions_tenant_id", table_name="actions")
    op.drop_index("ix_actions_status", table_name="actions")
    op.drop_index("ix_actions_parent_action_id", table_name="actions")
    op.drop_index("ix_actions_session_id", table_name="actions")
    op.drop_table("actions")

    # Drop the Postgres enum type if the bind supports it. No-op on SQLite.
    sa.Enum(*ACTION_STATUS_VALUES, name=ACTION_STATUS_NAME).drop(
        op.get_bind(), checkfirst=True
    )

    op.drop_index("ix_sessions_tenant_id", table_name="sessions")
    op.drop_index("ix_sessions_original_workbook_path", table_name="sessions")
    op.drop_table("sessions")
