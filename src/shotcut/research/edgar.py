"""Thin async wrapper over edgartools.

Exposes the four tools the Stage 7 researcher agent uses:
  - `find_company(ticker_or_name)` → CIK
  - `get_filings(cik, form, count)` → list[FilingMeta]
  - `get_financial_statements(cik, period)` → FinancialStatements
  - `get_filing_section(accession, section)` → text

edgartools is synchronous; every call here runs through
`asyncio.to_thread` so the orchestrator's event loop stays responsive
while the SEC request is in flight. Each call passes through the
injected `AsyncRateLimiter` so we never exceed SEC's 10 req/sec fair-
access policy.

The client is stateless apart from the rate limiter; tests substitute
it entirely by subclassing or by monkeypatching the methods on an
instance. No in-flight HTTP mocking is required for tests.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from shotcut.research.facts import FilingMeta, FinancialStatements
from shotcut.research.rate_limit import AsyncRateLimiter

log = logging.getLogger(__name__)


class EdgarClientError(Exception):
    """Raised when edgartools can't satisfy a call (network, parse failure,
    missing entity). Researcher agent surfaces as a tool_result error so
    the LLM can try a different approach."""


class EdgarClient:
    def __init__(
        self,
        *,
        identity: str,
        rate_limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self._identity = identity
        self._rate_limiter = rate_limiter or AsyncRateLimiter(rate_per_second=10.0)
        self._identity_set = False

    async def _ensure_identity(self) -> None:
        if self._identity_set:
            return
        # set_identity mutates edgar module-level state; only call once.
        from edgar import set_identity

        await asyncio.to_thread(set_identity, self._identity)
        self._identity_set = True

    # -----------------------------------------------------------------
    # Tool 1: find_company
    # -----------------------------------------------------------------

    async def find_company(self, ticker_or_name: str) -> str:
        """Resolve a ticker or name to a zero-padded CIK string."""
        await self._ensure_identity()
        await self._rate_limiter.acquire()
        try:
            from edgar import Company

            company = await asyncio.to_thread(Company, ticker_or_name)
        except Exception as exc:  # edgartools raises varied exceptions
            raise EdgarClientError(
                f"could not resolve {ticker_or_name!r}: {exc}"
            ) from exc
        cik_raw = getattr(company, "cik", None)
        if cik_raw is None:
            raise EdgarClientError(f"no CIK for {ticker_or_name!r}")
        # SEC CIKs are canonically 10-digit zero-padded; edgartools may
        # return an int.
        return str(cik_raw).zfill(10)

    # -----------------------------------------------------------------
    # Tool 2: get_filings
    # -----------------------------------------------------------------

    async def get_filings(
        self, cik: str, *, form: str = "10-K", count: int = 4
    ) -> list[FilingMeta]:
        """Return the most-recent `count` filings of `form` for `cik`."""
        await self._ensure_identity()
        await self._rate_limiter.acquire()
        try:
            from edgar import Company

            company = await asyncio.to_thread(Company, cik)
            filings = await asyncio.to_thread(
                lambda: list(company.get_filings(form=form).head(count))
            )
        except Exception as exc:
            raise EdgarClientError(
                f"could not list {form} filings for cik={cik}: {exc}"
            ) from exc
        return [_to_filing_meta(f) for f in filings]

    # -----------------------------------------------------------------
    # Tool 3: get_financial_statements
    # -----------------------------------------------------------------

    async def get_financial_statements(
        self, cik: str, *, period: str = "annual", periods: int = 4
    ) -> FinancialStatements:
        """Normalized income / balance / cash flow for `cik`.

        `period` is edgartools' `period` argument — `"annual"` or
        `"quarterly"`. `periods` is how many to fetch.
        """
        await self._ensure_identity()
        await self._rate_limiter.acquire()
        try:
            from edgar import Company

            company = await asyncio.to_thread(Company, cik)
            inc = await asyncio.to_thread(
                lambda: company.income_statement(
                    periods=periods, period=period, as_dataframe=True
                )
            )
            bal = await asyncio.to_thread(
                lambda: company.balance_sheet(
                    periods=periods, period=period, as_dataframe=True
                )
            )
            cf = await asyncio.to_thread(
                lambda: company.cash_flow_statement(
                    periods=periods, period=period, as_dataframe=True
                )
            )
            latest_filing = await asyncio.to_thread(
                lambda: next(iter(company.get_filings(form=["10-K", "10-Q"]).head(1)))
            )
        except Exception as exc:
            raise EdgarClientError(
                f"could not fetch statements for cik={cik}: {exc}"
            ) from exc

        # Narrow DataFrames to {metric: most-recent-period value}. Handles
        # the case where the returned object is None (the period argument
        # didn't match anything) by falling back to an empty dict.
        return FinancialStatements(
            cik=cik,
            ticker=getattr(company, "tickers", [None])[0],
            period=_extract_latest_period(inc),
            income_statement=_df_to_metric_dict(inc),
            balance_sheet=_df_to_metric_dict(bal),
            cash_flow=_df_to_metric_dict(cf),
            filing_accession=str(
                getattr(latest_filing, "accession_no", "") or ""
            ),
            filing_type=str(getattr(latest_filing, "form", "") or ""),
            filing_url=_filing_url(latest_filing),
        )

    # -----------------------------------------------------------------
    # Tool 4: get_filing_section
    # -----------------------------------------------------------------

    async def get_filing_section(
        self, accession: str, section: str, *, max_chars: int = 20_000
    ) -> str:
        """Return the text of `section` (MD&A, risk_factors, etc.) from
        the filing identified by `accession`. Text is truncated to
        `max_chars` — the researcher agent doesn't need the whole thing,
        and LLM prompts get expensive fast."""
        await self._ensure_identity()
        await self._rate_limiter.acquire()
        try:
            from edgar import find

            filing = await asyncio.to_thread(find, accession)
            obj = await asyncio.to_thread(filing.obj)
            text = await asyncio.to_thread(
                lambda: _extract_section_text(obj, section)
            )
        except Exception as exc:
            raise EdgarClientError(
                f"could not fetch section {section!r} of {accession}: {exc}"
            ) from exc
        if text is None:
            return f"(section {section!r} not available in {accession})"
        if len(text) > max_chars:
            return text[:max_chars] + "\n\n... (truncated)"
        return text


# ---------------------------------------------------------------------------
# edgartools ↔ our domain types (kept isolated so tests can mock the client
# without mocking the extraction helpers)
# ---------------------------------------------------------------------------


def _to_filing_meta(filing: Any) -> FilingMeta:
    return FilingMeta(
        accession=str(getattr(filing, "accession_no", "") or ""),
        form=str(getattr(filing, "form", "") or ""),
        filing_date=str(getattr(filing, "filing_date", "") or ""),
        url=_filing_url(filing),
    )


def _filing_url(filing: Any) -> str:
    """edgartools doesn't expose a single URL getter; construct one from
    the accession number. SEC filing index URLs have the form:
    https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&...

    We use the simple filing homepage URL instead since accession numbers
    are canonical.
    """
    accession = str(getattr(filing, "accession_no", "") or "")
    if not accession:
        return ""
    nodash = accession.replace("-", "")
    cik_int = int(getattr(filing, "cik", 0) or 0)
    return (
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={cik_int:010d}&type=&dateb=&owner=include&count=40"
        f"&action=getcompany&accession={accession}"
    ) if accession else f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash}/"


def _df_to_metric_dict(df: Any) -> dict[str, float | None]:
    """Turn a pandas-ish statement DataFrame into `{metric: value}`.

    edgartools statements are indexed by concept name; columns are
    periods. We take the most recent (first column) and coerce each
    row to a float. `None` for any row that isn't numeric.
    """
    if df is None:
        return {}
    try:
        columns = list(df.columns)
        if not columns:
            return {}
        first_period_col = columns[0]
        series = df[first_period_col]
        out: dict[str, float | None] = {}
        for metric, value in series.items():
            try:
                out[str(metric)] = float(value)
            except (TypeError, ValueError):
                out[str(metric)] = None
        return out
    except Exception as exc:
        log.debug("df_to_metric_dict: unexpected shape, returning empty: %s", exc)
        return {}


def _extract_latest_period(df: Any) -> str:
    if df is None:
        return ""
    try:
        columns = list(df.columns)
        return str(columns[0]) if columns else ""
    except Exception:
        return ""


def _extract_section_text(filing_obj: Any, section: str) -> str | None:
    """edgartools' TenK / TenQ objects expose sections as attributes
    (`mda`, `risk_factors`, ...) or through a `.sections` dict depending
    on version. Try a few shapes."""
    # Direct attribute access (TenK has .mda, .risk_factors, .business, etc.)
    direct = getattr(filing_obj, section, None)
    if isinstance(direct, str) and direct.strip():
        return direct
    sections = getattr(filing_obj, "sections", None)
    if isinstance(sections, dict) and section in sections:
        value = sections[section]
        return str(value) if value is not None else None
    return None
