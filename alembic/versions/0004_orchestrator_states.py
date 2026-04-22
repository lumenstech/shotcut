"""stage 6: orchestrator_states table for durable execution.

New table — durable state machine per active session. On worker boot,
the app scans non-terminal rows and resumes the orchestrator from the
checkpoint. See docs/decisions/0003-durable-execution.md.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ORCH_STATE_VALUES = ("planning", "executing", "verifying", "done", "failed")
ORCH_STATE_NAME = "orchestrator_state"


def upgrade() -> None:
    orch_state = sa.Enum(*ORCH_STATE_VALUES, name=ORCH_STATE_NAME)

    op.create_table(
        "orchestrator_states",
        sa.Column(
            "session_id",
            sa.Uuid,
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("state", orch_state, nullable=False),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("plan", sa.JSON, nullable=True),
        sa.Column(
            "current_step_index",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("error", sa.Text, nullable=True),
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
        "ix_orchestrator_states_state",
        "orchestrator_states",
        ["state"],
    )


def downgrade() -> None:
    op.drop_index("ix_orchestrator_states_state", table_name="orchestrator_states")
    op.drop_table("orchestrator_states")

    sa.Enum(*ORCH_STATE_VALUES, name=ORCH_STATE_NAME).drop(
        op.get_bind(), checkfirst=True
    )
