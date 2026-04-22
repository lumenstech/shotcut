"""stage 5 erratum: client_action_id on actions.

Erratum to Stage 3's consolidated schema. See
docs/decisions/0002-schema-consolidation.md → "Stage 5 erratum" for
the rationale — in short, Stage 4's verifier attribution work needs
a stable identity for a logical action that survives serialization,
branching, and multi-process checkpointing. Python's `id()` doesn't.

Migration path:
  1. Add `client_action_id` as nullable so existing rows can stay.
  2. Backfill every existing row with a fresh UUID.
  3. Flip the column to NOT NULL.
  4. Add an index — Stage 5's replay/branching query against it often.

Runs cleanly on:
  - A fresh DB: step 2 finds no rows, step 3 is a no-op flip.
  - A Stage 3/4 populated DB: every row gets a unique backfill UUID.

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-22
"""
from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add nullable column so existing rows don't violate NOT NULL mid-migration.
    op.add_column(
        "actions",
        sa.Column("client_action_id", sa.Uuid, nullable=True),
    )

    # 2. Backfill. We have to assign a distinct UUID per row; a single
    #    UPDATE with gen_random_uuid()-style defaults isn't portable
    #    across SQLite (test) and Postgres (prod), so issue one UPDATE
    #    per row using a correlated Python-side uuid4().
    bind = op.get_bind()
    row_ids = [
        r[0]
        for r in bind.execute(sa.text("SELECT id FROM actions")).fetchall()
    ]
    if row_ids:
        for row_id in row_ids:
            bind.execute(
                sa.text(
                    "UPDATE actions SET client_action_id = :cid WHERE id = :id"
                ),
                {"cid": str(uuid.uuid4()), "id": str(row_id)},
            )

    # 3. Flip to NOT NULL. SQLite's ALTER COLUMN is limited — Alembic's
    #    batch_alter_table recreates the table on SQLite and emits
    #    straight ALTER on Postgres.
    with op.batch_alter_table("actions") as batch:
        batch.alter_column("client_action_id", nullable=False)

    # 4. Index for Stage 5's replay / branch queries.
    op.create_index(
        "ix_actions_client_action_id",
        "actions",
        ["client_action_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_actions_client_action_id", table_name="actions")
    op.drop_column("actions", "client_action_id")
