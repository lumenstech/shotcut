# 0004 — Research Integration

**Status:** Accepted
**Date:** 2026-04-22
**Context:** Stage 7 of BUILD_PLAN.md — replace the researcher stub
with real SEC EDGAR integration.

## Decision

The researcher agent uses Haiku 4.5 with tool use over four
deterministic EDGAR tools, exposed through a thin `EdgarClient`
wrapper. Facts are cached in Postgres for 24 hours; rate limiting
honors SEC's 10 req/sec fair-access policy and identifies with a
contact email.

## Tool surface (contract with the LLM)

| Tool | Returns |
|---|---|
| `find_company(ticker_or_name)` | CIK string |
| `get_filings(cik, form, count)` | list of `FilingMeta(accession, form, date, url)` |
| `get_financial_statements(cik, period)` | normalized income / balance / cash-flow dicts |
| `get_filing_section(accession, section)` | text of the named section (MD&A, risk factors, etc.) |

These are the minimum set the agent needs to answer "pull AAPL's last
four quarters of revenue" and "what are the top three risk factors in
TSLA's most recent 10-K." Adding more tools later (full-text search,
comparables) is additive.

## Caching

`financial_facts` is a new table — not a column add to an existing
one, so the Stage 3 consolidation rule is preserved.

- **Key:** `(cik, statement, period, metric)` with a unique index.
- **TTL:** 24 hours for every fact at Stage 7. A stronger invalidation
  strategy (infinite-TTL for non-current periods keyed on "is this
  the most-recent period for the CIK?") is a Stage 10 concern; 24h is
  good enough until eval coverage says otherwise.
- **Provenance preserved:** every cached row carries
  `filing_accession`, `filing_type`, `filing_url` so workbook cells
  that reference a fact can cite the source.

## Rate limiting

Simple async token bucket, refill rate matching SEC's 10 req/sec. The
rate limiter is an instance attribute of `EdgarClient` so tests
inject a faster or mocked limiter. SEC also wants an identifying
`User-Agent` with a contact email — `settings.edgar_identity` already
exists and edgartools consumes it on `set_identity()`.

## Async wrap

edgartools is synchronous. `EdgarClient` wraps every call in
`asyncio.to_thread` so the orchestrator's event loop stays responsive
while the SEC request is in flight. No separate thread pool — the
default executor is fine for the request volumes we expect.

## Orchestrator integration — deferred

Stage 7 ships the researcher agent as a standalone callable. Wiring
it into the orchestrator's plan → execute loop (so the planner can
emit "research" steps that invoke the researcher before EXECUTING)
is a Stage 8+ concern: it needs a new step-type, a new orchestrator
phase (RESEARCHING), and a way for researcher output to reach the
executor. Stage 7's scope is "make the stub real," not "wire the
dependency graph."

## What Stage 7 does NOT introduce

- **No semantic search over filings.** We fetch named sections, not
  arbitrary queries. LLM-driven retrieval lives above this layer.
- **No automatic restatement handling.** Cached facts for a restated
  period go stale and are refreshed on the 24h boundary. Restatement
  detection needs a comparison pass that's out of scope here.
- **No cross-company comparables.** The agent handles one CIK per
  query; comparables (peer ratios, industry medians) would need a
  separate tool.
- **No column adds to existing tables.** `financial_facts` is a new
  table, consistent with the Stage 3 rule.
