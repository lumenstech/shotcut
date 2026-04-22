"""Researcher agent — Haiku 4.5 + tool use over the EDGAR client.

The Stage 7 replacement for the original "fetch-and-summarize" stub.
Claude Haiku 4.5 is given four deterministic tools (find_company,
get_filings, get_financial_statements, get_filing_section) and asked
to answer the caller's query. Output is a `ResearchResult` carrying
the facts it collected plus their provenance (filing accession + URL)
so workbook cells that reference a fact can cite the source.

Caching: the researcher consults `research.cache.get()` before each
`get_financial_statements` call and writes the result back via
`put_many()`. Rate limiting lives inside the `EdgarClient` so every
path honors SEC's 10 req/sec.

Scope: Stage 7 ships the researcher as a standalone callable. Wiring
it into the orchestrator's plan → execute loop (so the planner can
emit research steps) is a Stage 8+ concern — see
docs/decisions/0004-research-integration.md.
"""
from __future__ import annotations

import json
import logging
from typing import Any, cast

from anthropic.types import MessageParam, ToolParam, ToolResultBlockParam, ToolUseBlock
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.config import settings
from shotcut.llm.client import cached_system, get_client
from shotcut.research import cache
from shotcut.research.edgar import EdgarClient, EdgarClientError
from shotcut.research.facts import (
    FinancialFact,
    FinancialStatements,
    ResearchResult,
)

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """You help extract financial data from SEC filings.

You have four tools:
  - `find_company(ticker_or_name)` → CIK string. Start here when the
    user gives you a ticker or company name.
  - `get_filings(cik, form, count)` → list of filings for the CIK.
  - `get_financial_statements(cik, period)` → normalized income /
    balance / cash-flow dicts for the most recent reporting period.
    `period` is `"annual"` or `"quarterly"`.
  - `get_filing_section(accession, section)` → text of a named section
    (e.g. `"mda"`, `"risk_factors"`, `"business"`) from a specific filing.

Rules:
- Never guess a value. If a metric isn't present in the tool output,
  return null for it — do not infer from industry averages or prior
  periods.
- Always include provenance: every fact you return must name the
  filing accession number + filing type it came from.
- Prefer the most recent filing unless the user asks for historical
  comparisons. Use `get_filings` to list, then
  `get_financial_statements` for the numbers.
- When the user asks about risk or strategy, fetch the relevant
  section via `get_filing_section` and summarize.
- When you have the facts, stop calling tools and return a concise
  text summary.
"""


# ---------------------------------------------------------------------------
# Tool schemas the LLM sees
# ---------------------------------------------------------------------------

TOOLS: list[ToolParam] = [
    {
        "name": "find_company",
        "description": "Resolve a ticker symbol or company name to a zero-padded SEC CIK.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker_or_name": {"type": "string"},
            },
            "required": ["ticker_or_name"],
        },
    },
    {
        "name": "get_filings",
        "description": (
            "List the most recent filings of a given form for a CIK. "
            "Common forms: 10-K (annual), 10-Q (quarterly), 8-K (current "
            "events)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cik": {"type": "string"},
                "form": {"type": "string", "default": "10-K"},
                "count": {"type": "integer", "default": 4},
            },
            "required": ["cik"],
        },
    },
    {
        "name": "get_financial_statements",
        "description": (
            "Return normalized income / balance / cash-flow statements "
            "for the most recent reporting period of the given CIK. "
            "`period` is 'annual' or 'quarterly'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cik": {"type": "string"},
                "period": {
                    "type": "string",
                    "enum": ["annual", "quarterly"],
                    "default": "annual",
                },
                "periods": {"type": "integer", "default": 4},
            },
            "required": ["cik"],
        },
    },
    {
        "name": "get_filing_section",
        "description": (
            "Return the text of a named section from an SEC filing, "
            "identified by accession number. Common sections: 'mda', "
            "'risk_factors', 'business', 'properties'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "accession": {"type": "string"},
                "section": {"type": "string"},
            },
            "required": ["accession", "section"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool dispatch + cache integration
# ---------------------------------------------------------------------------


async def _dispatch_tool(
    db: AsyncSession,
    client: EdgarClient,
    name: str,
    inputs: dict[str, Any],
    *,
    collected: list[FinancialFact],
) -> Any:
    """Invoke the named tool; raise EdgarClientError on failure so the
    caller can surface it to the LLM via tool_result with is_error=True.

    Side effects:
    - Caches statements facts via `research.cache.put_many`.
    - Appends every FinancialFact we collect to `collected`, which
      becomes the researcher's final output.
    """
    if name == "find_company":
        return await client.find_company(inputs["ticker_or_name"])

    if name == "get_filings":
        filings = await client.get_filings(
            inputs["cik"],
            form=inputs.get("form", "10-K"),
            count=inputs.get("count", 4),
        )
        return [f.model_dump() for f in filings]

    if name == "get_financial_statements":
        cik = inputs["cik"]
        period = inputs.get("period", "annual")
        periods = inputs.get("periods", 4)
        statements = await client.get_financial_statements(
            cik, period=period, periods=periods
        )
        facts = _statements_to_facts(statements)
        if facts:
            collected.extend(facts)
            await cache.put_many(db, facts=facts)
            await db.commit()
        return statements.model_dump()

    if name == "get_filing_section":
        return await client.get_filing_section(
            inputs["accession"], inputs["section"]
        )

    raise ValueError(f"unknown researcher tool: {name!r}")


def _statements_to_facts(s: FinancialStatements) -> list[FinancialFact]:
    """Fan a FinancialStatements blob out into one FinancialFact per
    non-None metric. Skips None values so the cache doesn't get
    polluted with absences."""
    out: list[FinancialFact] = []
    for statement_name, metrics in (
        ("income_statement", s.income_statement),
        ("balance_sheet", s.balance_sheet),
        ("cash_flow", s.cash_flow),
    ):
        for metric, value in metrics.items():
            if value is None:
                continue
            out.append(
                FinancialFact(
                    cik=s.cik,
                    ticker=s.ticker,
                    statement=statement_name,  # type: ignore[arg-type]
                    period=s.period,
                    metric=metric,
                    value=value,
                    unit=None,
                    filing_accession=s.filing_accession,
                    filing_type=s.filing_type,
                    filing_url=s.filing_url,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def research(
    query: str,
    *,
    db: AsyncSession,
    edgar_client: EdgarClient | None = None,
    max_iterations: int = 8,
) -> ResearchResult:
    """Ask Haiku 4.5 to answer `query` using the SEC tools.

    `edgar_client` is injectable so tests (and future orchestrator
    callers) can pass a configured instance. When None, builds one
    from `settings.edgar_identity`.
    """
    client = get_client()
    edgar = edgar_client or EdgarClient(identity=settings.edgar_identity)

    collected: list[FinancialFact] = []
    messages: list[MessageParam] = [{"role": "user", "content": query}]

    for _ in range(max_iterations):
        response = await client.messages.create(
            model=settings.researcher_model,
            max_tokens=4000,
            system=cached_system(SYSTEM_PROMPT),
            tools=TOOLS,
            messages=messages,
        )

        tool_uses = [b for b in response.content if isinstance(b, ToolUseBlock)]
        if not tool_uses:
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_results: list[ToolResultBlockParam] = []
        for block in tool_uses:
            inputs = cast(dict[str, Any], block.input)
            try:
                result = await _dispatch_tool(
                    db, edgar, block.name, inputs, collected=collected
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    }
                )
            except EdgarClientError as exc:
                log.info("researcher: tool %s failed: %s", block.name, exc)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"tool error: {exc}",
                        "is_error": True,
                    }
                )
            except Exception as exc:  # noqa: BLE001 — dispatch is all we own
                log.exception("researcher: unexpected error in %s", block.name)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"internal error: {type(exc).__name__}: {exc}",
                        "is_error": True,
                    }
                )

        messages.append({"role": "user", "content": tool_results})

        if response.stop_reason == "end_turn":
            break

    summary_parts = [b.text for b in response.content if hasattr(b, "text")]
    summary = "\n".join(t for t in summary_parts if t)

    return ResearchResult(
        ticker=collected[0].ticker if collected else None,
        cik=collected[0].cik if collected else None,
        summary=summary,
        facts=collected,
    )
