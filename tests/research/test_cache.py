"""Postgres cache for financial facts — put/get + expiry semantics."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.research import cache
from shotcut.research.facts import FinancialFact


def _fact(
    *,
    metric: str = "Revenue",
    value: float = 100.0,
    period: str = "FY2024",
    cik: str = "0000320193",
) -> FinancialFact:
    return FinancialFact(
        cik=cik,
        ticker="AAPL",
        statement="income_statement",
        period=period,
        metric=metric,
        value=value,
        unit="USD",
        filing_accession="0000320193-24-000001",
        filing_type="10-K",
        filing_url="https://www.sec.gov/fake",
    )


@pytest_asyncio.fixture
async def db(test_db: AsyncSession) -> AsyncSession:
    return test_db


async def test_get_returns_none_for_missing_key(db: AsyncSession) -> None:
    result = await cache.get(
        db,
        cik="0000320193",
        statement="income_statement",
        period="FY2024",
        metric="Revenue",
    )
    assert result is None


async def test_put_then_get_round_trips(db: AsyncSession) -> None:
    original = _fact(value=394_328_000_000.0)
    await cache.put(db, fact=original)
    await db.commit()

    fetched = await cache.get(
        db,
        cik=original.cik,
        statement=original.statement,
        period=original.period,
        metric=original.metric,
    )
    assert fetched is not None
    assert fetched.value == 394_328_000_000.0
    assert fetched.filing_accession == original.filing_accession
    assert fetched.ticker == "AAPL"


async def test_put_upserts_existing_row(db: AsyncSession) -> None:
    """Writing the same key twice updates in place rather than duplicating."""
    await cache.put(db, fact=_fact(value=1.0))
    await cache.put(db, fact=_fact(value=2.0))
    await db.commit()

    fetched = await cache.get(
        db,
        cik="0000320193",
        statement="income_statement",
        period="FY2024",
        metric="Revenue",
    )
    assert fetched is not None
    assert fetched.value == 2.0


async def test_expired_row_is_not_returned(db: AsyncSession) -> None:
    frozen = datetime(2026, 4, 22, tzinfo=timezone.utc)
    await cache.put(
        db, fact=_fact(value=10.0), ttl=timedelta(hours=1), now=frozen
    )
    await db.commit()

    # Query "a day later" → past the 1h TTL.
    later = frozen + timedelta(days=1)
    fetched = await cache.get(
        db,
        cik="0000320193",
        statement="income_statement",
        period="FY2024",
        metric="Revenue",
        now=later,
    )
    assert fetched is None


async def test_ttl_none_never_expires(db: AsyncSession) -> None:
    """ttl=None is the "confirmed prior period" case — no expiration."""
    frozen = datetime(2026, 4, 22, tzinfo=timezone.utc)
    await cache.put(
        db, fact=_fact(value=10.0, period="FY2019"), ttl=None, now=frozen
    )
    await db.commit()

    far_future = frozen + timedelta(days=365 * 5)
    fetched = await cache.get(
        db,
        cik="0000320193",
        statement="income_statement",
        period="FY2019",
        metric="Revenue",
        now=far_future,
    )
    assert fetched is not None


async def test_put_many_batches_upserts(db: AsyncSession) -> None:
    facts = [
        _fact(metric="Revenue", value=100.0),
        _fact(metric="NetIncome", value=30.0),
        _fact(metric="OperatingIncome", value=40.0),
    ]
    await cache.put_many(db, facts=facts)
    await db.commit()

    for fact in facts:
        fetched = await cache.get(
            db,
            cik=fact.cik,
            statement=fact.statement,
            period=fact.period,
            metric=fact.metric,
        )
        assert fetched is not None
        assert fetched.value == fact.value


async def test_put_many_empty_is_noop(db: AsyncSession) -> None:
    # No exception; no rows inserted.
    await cache.put_many(db, facts=[])
    await db.commit()


async def test_different_periods_are_distinct_entries(db: AsyncSession) -> None:
    """The uniqueness constraint is on the quadruple — not cik alone."""
    await cache.put(db, fact=_fact(period="FY2023", value=50.0))
    await cache.put(db, fact=_fact(period="FY2024", value=100.0))
    await db.commit()

    f2023 = await cache.get(
        db, cik="0000320193", statement="income_statement",
        period="FY2023", metric="Revenue",
    )
    f2024 = await cache.get(
        db, cik="0000320193", statement="income_statement",
        period="FY2024", metric="Revenue",
    )
    assert f2023 is not None and f2023.value == 50.0
    assert f2024 is not None and f2024.value == 100.0
