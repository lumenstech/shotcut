"""UserContext and tenant scoping helpers.

The `UserContext` is the one-and-only identity blob that flows from
the auth middleware to every downstream layer (routes → orchestrator →
audit.record). Nothing constructs one ad-hoc; it's produced by
`middleware.get_current_user` and threaded explicitly as a dep
parameter so you can always grep the call graph.

`scope_to_tenant` sets the Postgres session variable that the Stage 8
RLS policies key on. It's a no-op on SQLite (no RLS support), matching
the migration's dialect-conditional policy creation.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class UserContext:
    """Authenticated request identity.

    `sub` is the JWT subject claim (stable per-user identifier from the
    IdP). `tenant_id` comes from the configured custom claim namespace.
    Both are required for authenticated routes; construction-time
    validation lives in the middleware, not here.
    """

    sub: str
    tenant_id: uuid.UUID

    @classmethod
    def anonymous(cls) -> "UserContext":
        """Fallback identity used when auth is disabled (dev/test only).

        The tenant_id is a fixed sentinel UUID so anonymous writes land
        in a clearly-labeled tenant partition that production RLS will
        deny access to.
        """
        return cls(
            sub="anonymous",
            tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
        )


async def scope_to_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Set `app.tenant_id` on the DB session so Postgres RLS policies fire.

    No-op on SQLite: SQLite has no RLS support, and the Stage 8
    migration skips policy creation on non-pg dialects. The
    application-level `.where(X.tenant_id == user.tenant_id)` filters
    are authoritative on SQLite; RLS is defense-in-depth in production.
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return
    # set_config(name, value, is_local) — is_local=true so the setting
    # vanishes at commit/rollback boundary and doesn't leak to the next
    # checkout of this connection from the pool.
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )
