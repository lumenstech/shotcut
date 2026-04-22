"""stage 7: financial_facts cache table.

New table caching facts fetched from SEC EDGAR, keyed on
`(cik, statement, period, metric)`. Not a column add to an existing
table — the Stage 3 consolidation rule stands. See
docs/decisions/0004-research-integration.md.

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Unique constraint is declared inline so SQLite (test) can apply it
    # without ALTER TABLE — Postgres runs the same create_table happily.
    op.create_table(
        "financial_facts",
        sa.Column("id", sa.Uuid, primary_key=True),
        # Subject identity.
        sa.Column("cik", sa.String(16), nullable=False),
        sa.Column("ticker", sa.String(16), nullable=True),
        # Fact identity (unique with cik).
        sa.Column("statement", sa.String(32), nullable=False),
        sa.Column("period", sa.String(32), nullable=False),
        sa.Column("metric", sa.String(128), nullable=False),
        # Value + unit (Numeric avoids float drift on currency amounts).
        sa.Column("value", sa.Numeric(30, 10), nullable=False),
        sa.Column("unit", sa.String(32), nullable=True),
        # Provenance — required so workbook cells can cite sources.
        sa.Column("filing_accession", sa.String(64), nullable=False),
        sa.Column("filing_type", sa.String(16), nullable=False),
        sa.Column("filing_url", sa.String(512), nullable=False),
        # Cache metadata.
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "cik", "statement", "period", "metric",
            name="uq_financial_facts_key",
        ),
    )
    op.create_index("ix_financial_facts_cik", "financial_facts", ["cik"])
    op.create_index(
        "ix_financial_facts_expires_at", "financial_facts", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_financial_facts_expires_at", table_name="financial_facts")
    op.drop_index("ix_financial_facts_cik", table_name="financial_facts")
    op.drop_table("financial_facts")
