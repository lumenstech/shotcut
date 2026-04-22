"""Researcher agent.

Fetches external data (SEC filings via edgartools) when the planner
determines a step needs live financial inputs. Kept minimal: one entry
point that takes a natural-language data need and returns a structured
summary the executor can reference.
"""
from __future__ import annotations

from pydantic import BaseModel

from shotcut.config import settings
from shotcut.data import edgar
from shotcut.llm.client import cached_system, get_client

SYSTEM_PROMPT = """You help extract financial data from SEC filings.

You are given a ticker symbol and an excerpt of financial statements. \
Return the specific metrics the caller asked for. If a metric is not \
present in the excerpt, set its value to null — do not guess.
"""


class Metric(BaseModel):
    name: str
    value: float | None
    period: str | None = None
    unit: str | None = None


class ResearchResult(BaseModel):
    ticker: str
    source: str
    metrics: list[Metric]


async def research(ticker: str, metric_names: list[str]) -> ResearchResult:
    filing_excerpt = edgar.latest_financials_excerpt(ticker)

    client = get_client()
    user_message = (
        f"Ticker: {ticker}\n"
        f"Metrics requested: {', '.join(metric_names)}\n\n"
        f"Filing excerpt:\n{filing_excerpt}"
    )
    response = await client.messages.parse(
        model=settings.researcher_model,
        max_tokens=4000,
        system=cached_system(SYSTEM_PROMPT),
        messages=[{"role": "user", "content": user_message}],
        output_format=ResearchResult,
    )
    result = response.parsed_output
    if result is None:
        raise RuntimeError("researcher: model returned no parseable output")
    return result
