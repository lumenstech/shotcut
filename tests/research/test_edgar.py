"""EdgarClient tests — edgartools is mocked at import.

The client wraps every edgartools call in `asyncio.to_thread`; we
patch the imports inside each method so real SEC requests never fire.
Rate-limiter integration is tested end-to-end (every method awaits the
limiter before proceeding).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from shotcut.research.edgar import EdgarClient, EdgarClientError
from shotcut.research.rate_limit import AsyncRateLimiter


# ---------------------------------------------------------------------------
# Fake edgartools surface
# ---------------------------------------------------------------------------


class FakeFiling:
    def __init__(
        self,
        *,
        accession: str,
        form: str,
        filing_date: str,
        cik: int,
    ) -> None:
        self.accession_no = accession
        self.form = form
        self.filing_date = filing_date
        self.cik = cik


class FakeFilings:
    """Mimics edgartools' CompanyFilings slice shape (`.head(n)` returns
    an iterable)."""

    def __init__(self, filings: list[FakeFiling]) -> None:
        self._filings = filings

    def head(self, n: int) -> list[FakeFiling]:
        return self._filings[:n]


class FakeCompany:
    def __init__(
        self,
        *,
        cik: int,
        tickers: list[str] | None = None,
        filings: list[FakeFiling] | None = None,
        income_df: Any = None,
        balance_df: Any = None,
        cashflow_df: Any = None,
    ) -> None:
        self.cik = cik
        self.tickers = tickers or []
        self._filings = filings or []
        self._income = income_df
        self._balance = balance_df
        self._cashflow = cashflow_df

    def get_filings(self, form: Any = None) -> FakeFilings:
        if isinstance(form, list):
            filings = [f for f in self._filings if f.form in form]
        elif form:
            filings = [f for f in self._filings if f.form == form]
        else:
            filings = list(self._filings)
        return FakeFilings(filings)

    def income_statement(self, **_kwargs: Any) -> Any:
        return self._income

    def balance_sheet(self, **_kwargs: Any) -> Any:
        return self._balance

    def cash_flow_statement(self, **_kwargs: Any) -> Any:
        return self._cashflow


class FakeDataFrame:
    """Tiny DataFrame lookalike: .columns (list) + __getitem__(col) → series."""

    def __init__(self, rows: dict[str, dict[str, float | None]]) -> None:
        self._rows = rows
        self.columns = list({k for inner in rows.values() for k in inner.keys()})

    def __getitem__(self, column: str) -> "FakeSeries":
        return FakeSeries({metric: vals.get(column) for metric, vals in self._rows.items()})


class FakeSeries:
    def __init__(self, values: dict[str, float | None]) -> None:
        self._values = values

    def items(self):
        return self._values.items()


# ---------------------------------------------------------------------------
# Patching edgartools imports inside EdgarClient methods
# ---------------------------------------------------------------------------


def _patch_edgar(monkeypatch: pytest.MonkeyPatch, **handlers: Any) -> None:
    """Inject a fake `edgar` module that provides whatever handlers the
    tests supply (Company, set_identity, find)."""
    fake = SimpleNamespace(
        set_identity=handlers.get("set_identity", lambda _: None),
        Company=handlers.get("Company", lambda _: None),
        find=handlers.get("find", lambda _: None),
    )
    import sys

    monkeypatch.setitem(sys.modules, "edgar", fake)


def _fast_limiter() -> AsyncRateLimiter:
    # High rate + no sleep matters to these tests — we're not testing the
    # limiter here, just confirming each call awaits it.
    return AsyncRateLimiter(rate_per_second=1000.0)


# ---------------------------------------------------------------------------
# find_company
# ---------------------------------------------------------------------------


async def test_find_company_returns_padded_cik(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_edgar(
        monkeypatch,
        Company=lambda _ticker: FakeCompany(cik=320193),
    )

    client = EdgarClient(identity="test dev@example.com", rate_limiter=_fast_limiter())
    cik = await client.find_company("AAPL")
    assert cik == "0000320193"


async def test_find_company_raises_on_edgartools_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_ticker: str) -> FakeCompany:
        raise RuntimeError("network down")

    _patch_edgar(monkeypatch, Company=boom)
    client = EdgarClient(identity="test dev@example.com", rate_limiter=_fast_limiter())
    with pytest.raises(EdgarClientError, match="could not resolve"):
        await client.find_company("AAPL")


# ---------------------------------------------------------------------------
# get_filings
# ---------------------------------------------------------------------------


async def test_get_filings_shapes_into_filing_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filings = [
        FakeFiling(
            accession="0000320193-24-000001",
            form="10-K",
            filing_date="2024-11-01",
            cik=320193,
        ),
        FakeFiling(
            accession="0000320193-23-000001",
            form="10-K",
            filing_date="2023-11-01",
            cik=320193,
        ),
    ]
    _patch_edgar(
        monkeypatch,
        Company=lambda _c: FakeCompany(cik=320193, filings=filings),
    )

    client = EdgarClient(identity="t x@y.z", rate_limiter=_fast_limiter())
    out = await client.get_filings("0000320193", form="10-K", count=2)

    assert [f.accession for f in out] == [
        "0000320193-24-000001",
        "0000320193-23-000001",
    ]
    assert all(f.form == "10-K" for f in out)
    assert all(f.url for f in out)


# ---------------------------------------------------------------------------
# get_financial_statements
# ---------------------------------------------------------------------------


async def test_get_financial_statements_normalizes_dataframes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inc = FakeDataFrame(
        {
            "Revenue": {"FY2024": 394_328_000_000.0},
            "NetIncome": {"FY2024": 96_995_000_000.0},
        }
    )
    bal = FakeDataFrame({"Assets": {"FY2024": 353_514_000_000.0}})
    cf = FakeDataFrame({"OperatingCashFlow": {"FY2024": 118_254_000_000.0}})
    filings = [
        FakeFiling(
            accession="0000320193-24-000001",
            form="10-K",
            filing_date="2024-11-01",
            cik=320193,
        )
    ]
    _patch_edgar(
        monkeypatch,
        Company=lambda _c: FakeCompany(
            cik=320193,
            tickers=["AAPL"],
            filings=filings,
            income_df=inc,
            balance_df=bal,
            cashflow_df=cf,
        ),
    )

    client = EdgarClient(identity="t x@y.z", rate_limiter=_fast_limiter())
    statements = await client.get_financial_statements("0000320193")

    assert statements.cik == "0000320193"
    assert statements.ticker == "AAPL"
    assert statements.period == "FY2024"
    assert statements.income_statement["Revenue"] == 394_328_000_000.0
    assert statements.income_statement["NetIncome"] == 96_995_000_000.0
    assert statements.balance_sheet["Assets"] == 353_514_000_000.0
    assert statements.cash_flow["OperatingCashFlow"] == 118_254_000_000.0
    assert statements.filing_accession == "0000320193-24-000001"
    assert statements.filing_type == "10-K"


# ---------------------------------------------------------------------------
# get_filing_section
# ---------------------------------------------------------------------------


async def test_get_filing_section_returns_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeObj:
        mda = "Our business did well this year."

    class FakeFound:
        def obj(self) -> FakeObj:
            return FakeObj()

    _patch_edgar(monkeypatch, find=lambda _acc: FakeFound())

    client = EdgarClient(identity="t x@y.z", rate_limiter=_fast_limiter())
    text = await client.get_filing_section("0000320193-24-000001", "mda")
    assert "did well" in text


async def test_get_filing_section_truncates_long_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_text = "x" * 100_000

    class FakeObj:
        risk_factors = long_text

    class FakeFound:
        def obj(self) -> FakeObj:
            return FakeObj()

    _patch_edgar(monkeypatch, find=lambda _acc: FakeFound())

    client = EdgarClient(identity="t x@y.z", rate_limiter=_fast_limiter())
    text = await client.get_filing_section(
        "0000320193-24-000001", "risk_factors", max_chars=1000
    )
    assert text.endswith("(truncated)")
    assert len(text) <= 1100  # 1000 + "\n\n... (truncated)"


async def test_get_filing_section_returns_sentinel_for_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeObj:
        pass  # no attributes

    class FakeFound:
        def obj(self) -> FakeObj:
            return FakeObj()

    _patch_edgar(monkeypatch, find=lambda _acc: FakeFound())

    client = EdgarClient(identity="t x@y.z", rate_limiter=_fast_limiter())
    text = await client.get_filing_section("acc", "nonexistent_section")
    assert "not available" in text


# ---------------------------------------------------------------------------
# Rate limiter is awaited on every call
# ---------------------------------------------------------------------------


async def test_every_call_awaits_rate_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = {"n": 0}

    class CountingLimiter(AsyncRateLimiter):
        def __init__(self) -> None:
            super().__init__(rate_per_second=1000.0)

        async def acquire(self) -> None:  # type: ignore[override]
            call_count["n"] += 1
            await super().acquire()

    _patch_edgar(
        monkeypatch,
        Company=lambda _c: FakeCompany(cik=320193, filings=[]),
    )

    client = EdgarClient(identity="t x@y.z", rate_limiter=CountingLimiter())
    await client.find_company("AAPL")
    await client.get_filings("0000320193", form="10-K", count=2)
    assert call_count["n"] == 2
