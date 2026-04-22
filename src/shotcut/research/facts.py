"""Pydantic models for research results.

Distinct from the SQLAlchemy `FinancialFact` ORM row in `db.models` —
that one is the persistence shape; these are the in-memory shapes the
researcher agent and tool responses use.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


Statement = Literal["income_statement", "balance_sheet", "cash_flow"]


class FilingMeta(BaseModel):
    """Summary of a single SEC filing. Used by `get_filings` tool."""

    accession: str  # e.g. "0000320193-24-000123"
    form: str  # "10-K", "10-Q", "8-K", ...
    filing_date: str  # ISO date, "2024-05-02"
    url: str  # link to the filing index page on SEC


class FinancialFact(BaseModel):
    """One metric from one filing for one period. Carries its provenance."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    cik: str
    ticker: str | None = None
    statement: Statement
    period: str
    metric: str
    value: float
    unit: str | None = None

    filing_accession: str
    filing_type: str
    filing_url: str
    fetched_at: datetime | None = None


class FinancialStatements(BaseModel):
    """Normalized income / balance / cash flow for a single `(cik, period)`.

    Each dict maps metric name → numeric value. `None` where the filing
    didn't report the metric (different companies surface different
    line items; the agent shouldn't infer zero from absence).
    """

    cik: str
    ticker: str | None = None
    period: str
    income_statement: dict[str, float | None]
    balance_sheet: dict[str, float | None]
    cash_flow: dict[str, float | None]

    filing_accession: str
    filing_type: str
    filing_url: str


class ResearchResult(BaseModel):
    """Final output of the researcher agent: the facts the LLM collected
    plus a short human-readable summary of what it did."""

    ticker: str | None = None
    cik: str | None = None
    summary: str
    facts: list[FinancialFact]
