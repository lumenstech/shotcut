"""Postgres cache of EDGAR-sourced financial facts.

Keyed on `(cik, statement, period, metric)`. 24h TTL at Stage 7;
a stronger invalidation policy (infinite TTL for confirmed non-current
periods) is deferred to Stage 10 per
docs/decisions/0004-research-integration.md.

Cache semantics:
- `get(...)` returns a `FinancialFact` if present AND `expires_at`
  hasn't passed. Expired rows are ignored (not actively evicted —
  the next write via `put()` upserts them).
- `put(fact, ttl)` upserts the row. `ttl=None` disables expiry
  (reserved for Stage 10).
- `put_many(facts, ttl)` batch-upserts a run of related facts
  (e.g. every metric from a statements call).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.db.models import FinancialFact as FinancialFactRow
from shotcut.research.facts import FinancialFact

DEFAULT_TTL = timedelta(hours=24)


async def get(
    db: AsyncSession,
    *,
    cik: str,
    statement: str,
    period: str,
    metric: str,
    now: datetime | None = None,
) -> FinancialFact | None:
    """Return the cached fact for the key if present and not expired."""
    current = now or datetime.now(timezone.utc)
    stmt = (
        select(FinancialFactRow)
        .where(FinancialFactRow.cik == cik)
        .where(FinancialFactRow.statement == statement)
        .where(FinancialFactRow.period == period)
        .where(FinancialFactRow.metric == metric)
        .limit(1)
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    expires_at = row.expires_at
    if expires_at is not None:
        # SQLite (tests) strips tz info on persistence; normalize before
        # comparing to our tz-aware `current` reference so expiry checks
        # are portable across dialects.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= current:
            return None
    return _row_to_fact(row)


async def put(
    db: AsyncSession,
    *,
    fact: FinancialFact,
    ttl: timedelta | None = DEFAULT_TTL,
    now: datetime | None = None,
) -> None:
    """Upsert one fact. `ttl=None` disables expiry."""
    await put_many(db, facts=[fact], ttl=ttl, now=now)


async def put_many(
    db: AsyncSession,
    *,
    facts: list[FinancialFact],
    ttl: timedelta | None = DEFAULT_TTL,
    now: datetime | None = None,
) -> None:
    """Upsert many facts in one round-trip."""
    if not facts:
        return
    current = now or datetime.now(timezone.utc)
    expires_at = current + ttl if ttl is not None else None

    rows: list[dict[str, object]] = []
    for fact in facts:
        rows.append(
            {
                "cik": fact.cik,
                "ticker": fact.ticker,
                "statement": fact.statement,
                "period": fact.period,
                "metric": fact.metric,
                "value": Decimal(str(fact.value)),
                "unit": fact.unit,
                "filing_accession": fact.filing_accession,
                "filing_type": fact.filing_type,
                "filing_url": fact.filing_url,
                "fetched_at": current,
                "expires_at": expires_at,
            }
        )

    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    # The set_ payload is identical across dialects — extracted here so
    # mypy sees a single assignment target per branch (pg and sqlite
    # Insert types are not interchangeable).
    excluded_fields = (
        "ticker", "value", "unit",
        "filing_accession", "filing_type", "filing_url",
        "fetched_at", "expires_at",
    )
    if dialect == "postgresql":
        pg_stmt = pg_insert(FinancialFactRow).values(rows)
        await db.execute(
            pg_stmt.on_conflict_do_update(
                constraint="uq_financial_facts_key",
                set_={k: getattr(pg_stmt.excluded, k) for k in excluded_fields},
            )
        )
    else:  # sqlite (tests)
        sqlite_stmt = sqlite_insert(FinancialFactRow).values(rows)
        await db.execute(
            sqlite_stmt.on_conflict_do_update(
                index_elements=["cik", "statement", "period", "metric"],
                set_={k: getattr(sqlite_stmt.excluded, k) for k in excluded_fields},
            )
        )


def _row_to_fact(row: FinancialFactRow) -> FinancialFact:
    return FinancialFact(
        cik=row.cik,
        ticker=row.ticker,
        statement=row.statement,  # type: ignore[arg-type]
        period=row.period,
        metric=row.metric,
        value=float(row.value),
        unit=row.unit,
        filing_accession=row.filing_accession,
        filing_type=row.filing_type,
        filing_url=row.filing_url,
        fetched_at=row.fetched_at,
    )
