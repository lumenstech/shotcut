"""stage 8: row-level security policies on tenant-scoped tables.

Postgres-only. Each tenant-scoped table gets:
  - `ALTER TABLE ... ENABLE ROW LEVEL SECURITY`
  - `CREATE POLICY tenant_isolation ON ... USING (
       tenant_id = current_setting('app.tenant_id', true)::uuid
       OR current_setting('app.tenant_id', true) = ''
    )`

The `current_setting('app.tenant_id', true) = ''` clause is a
deliberate escape hatch for background jobs and maintenance tasks
that run without a tenant context — those skip RLS entirely. In
production the application always sets the var before any query
(see `auth.tenancy.scope_to_tenant`), so the escape hatch only fires
for administrative connections.

SQLite has no RLS support. The migration detects the dialect and
skips policy creation on non-pg — tests stay green, application-level
`.where(X.tenant_id == user.tenant_id)` filters are authoritative on
SQLite.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-22
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables that carry `tenant_id` and need RLS. `financial_facts` is
# deliberately excluded — it caches public SEC data, shared across
# tenants. Kept in one list so future tenant-scoped tables only need
# to be added here.
_TENANT_TABLES: tuple[str, ...] = (
    "sessions",
    "actions",
    "session_branches",
)


def _is_postgres() -> bool:
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        # SQLite / other dialects: RLS not supported. App-level tenant
        # filtering is authoritative on those backends.
        return

    # session_branches gets tenant_id late-bound — it's a Stage 5 table
    # that didn't acquire the column at ship. Add it nullable so
    # existing rows don't violate; the app-level filter treats NULL as
    # "no tenant" / global.
    #
    # NB: `sessions` and `actions` already carry `tenant_id` from
    # migration 0001.
    op.execute(
        "ALTER TABLE session_branches "
        "ADD COLUMN IF NOT EXISTS tenant_id UUID"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_session_branches_tenant_id "
        "ON session_branches (tenant_id)"
    )

    for table in _TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (
                tenant_id = current_setting('app.tenant_id', true)::uuid
                OR current_setting('app.tenant_id', true) = ''
            )
            """
        )


def downgrade() -> None:
    if not _is_postgres():
        return
    for table in _TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    # Don't drop the tenant_id columns we added on session_branches /
    # financial_facts — that would be destructive of audit data. Leaving
    # them in place on downgrade is intentional.
