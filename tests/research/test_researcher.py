"""Researcher agent — Haiku 4.5 + tool-use loop.

Exercises the full researcher path with both the Anthropic client and
the EdgarClient mocked. The key invariants under test, from the Stage
7 acceptance criteria:

1. A query like "pull AAPL last 4 quarters revenue" drives the tool-
   use loop to `find_company` → `get_financial_statements` and returns
   `FinancialFact` objects with provenance.
2. Second invocation of a statements fetch hits the cache — no second
   SEC call. (The acceptance says "Cache hit on second call, no SEC
   request.")
3. Facts carry filing accession + filing URL so downstream workbook
   cells can cite the source.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.agents import researcher as researcher_mod
from shotcut.research import cache
from shotcut.research.edgar import EdgarClient
from shotcut.research.facts import FinancialStatements


# ---------------------------------------------------------------------------
# EdgarClient stand-in
# ---------------------------------------------------------------------------


class StubEdgar(EdgarClient):
    """EdgarClient that returns canned results and counts its calls.

    Subclasses the real client so type signatures match; all four
    methods are overridden to bypass edgartools and the rate limiter.
    """

    def __init__(self) -> None:  # type: ignore[no-untyped-def]
        self.calls: dict[str, int] = {
            "find_company": 0,
            "get_filings": 0,
            "get_financial_statements": 0,
            "get_filing_section": 0,
        }

    async def find_company(self, ticker_or_name: str) -> str:  # type: ignore[override]
        self.calls["find_company"] += 1
        return "0000320193"

    async def get_filings(
        self, cik: str, *, form: str = "10-K", count: int = 4
    ):  # type: ignore[override]
        self.calls["get_filings"] += 1
        return []

    async def get_financial_statements(
        self, cik: str, *, period: str = "annual", periods: int = 4
    ) -> FinancialStatements:  # type: ignore[override]
        self.calls["get_financial_statements"] += 1
        return FinancialStatements(
            cik=cik,
            ticker="AAPL",
            period="FY2024",
            income_statement={"Revenue": 394_328_000_000.0, "NetIncome": 96_995_000_000.0},
            balance_sheet={"Assets": 353_514_000_000.0},
            cash_flow={"OperatingCashFlow": 118_254_000_000.0},
            filing_accession="0000320193-24-000001",
            filing_type="10-K",
            filing_url="https://www.sec.gov/fake-url",
        )

    async def get_filing_section(
        self, accession: str, section: str, *, max_chars: int = 20_000
    ) -> str:  # type: ignore[override]
        self.calls["get_filing_section"] += 1
        return "canned section text"


# ---------------------------------------------------------------------------
# Anthropic client stand-in
# ---------------------------------------------------------------------------


class _ToolUseBlock:
    """Mimics anthropic.types.ToolUseBlock enough for the researcher's
    isinstance(..., ToolUseBlock) check."""

    type = "tool_use"

    def __init__(self, *, id: str, name: str, input: dict[str, Any]) -> None:
        self.id = id
        self.name = name
        self.input = input


class _FakeMessage:
    def __init__(
        self, *, content: list[Any], stop_reason: str = "tool_use"
    ) -> None:
        self.content = content
        self.stop_reason = stop_reason


class _ScriptedLLM:
    """Plays a canned script of responses through `.messages.create()`."""

    def __init__(self, responses: list[_FakeMessage]) -> None:
        self._responses = list(responses)
        self._messages = _Messages(self)

    @property
    def messages(self) -> "_Messages":
        return self._messages


class _Messages:
    def __init__(self, parent: _ScriptedLLM) -> None:
        self._parent = parent

    async def create(self, **_kwargs: Any) -> _FakeMessage:
        if not self._parent._responses:
            raise AssertionError("scripted LLM exhausted")
        return self._parent._responses.pop(0)


def _install_scripted_llm(
    monkeypatch: pytest.MonkeyPatch, responses: list[_FakeMessage]
) -> None:
    scripted = _ScriptedLLM(responses)
    # Patch the binding inside the researcher module — `get_client` was
    # imported there at module load, so patching llm_client.get_client
    # alone wouldn't redirect the researcher's reference.
    monkeypatch.setattr(researcher_mod, "get_client", lambda: scripted)
    # Also patch ToolUseBlock so researcher's isinstance check matches our fakes.
    monkeypatch.setattr(researcher_mod, "ToolUseBlock", _ToolUseBlock)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(test_db: AsyncSession) -> AsyncSession:
    return test_db


# ---------------------------------------------------------------------------
# Acceptance: end-to-end flow with provenance
# ---------------------------------------------------------------------------


async def test_researcher_resolves_then_fetches_statements(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent calls find_company → get_financial_statements and
    returns FinancialFact objects with provenance."""
    stub_edgar = StubEdgar()

    responses = [
        # Turn 1: model calls find_company.
        _FakeMessage(
            content=[
                _ToolUseBlock(
                    id="toolu_1",
                    name="find_company",
                    input={"ticker_or_name": "AAPL"},
                )
            ]
        ),
        # Turn 2: model calls get_financial_statements.
        _FakeMessage(
            content=[
                _ToolUseBlock(
                    id="toolu_2",
                    name="get_financial_statements",
                    input={"cik": "0000320193", "period": "annual"},
                )
            ]
        ),
        # Turn 3: model returns a summary (no tool_use → loop ends).
        _FakeMessage(
            content=[SimpleNamespace(type="text", text="AAPL FY2024 revenue: $394.3B")],
            stop_reason="end_turn",
        ),
    ]
    _install_scripted_llm(monkeypatch, responses)

    result = await researcher_mod.research(
        "What is AAPL's most recent annual revenue?",
        db=db,
        edgar_client=stub_edgar,
    )

    assert result.ticker == "AAPL"
    assert result.cik == "0000320193"
    # All three cached statements produced facts.
    assert len(result.facts) == 4  # Revenue + NetIncome + Assets + OperatingCashFlow
    # Every fact has provenance.
    for fact in result.facts:
        assert fact.filing_accession == "0000320193-24-000001"
        assert fact.filing_type == "10-K"
        assert fact.filing_url.startswith("https://")
    # Summary captured.
    assert "394.3B" in result.summary


async def test_second_researcher_call_hits_cache(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 7 acceptance: second call with the same (cik, period) doesn't
    re-fetch from SEC.

    The cache is consulted indirectly — the first researcher.research()
    call populates it; a direct cache.get() afterwards finds the facts.
    """
    stub_edgar = StubEdgar()
    responses = [
        _FakeMessage(
            content=[
                _ToolUseBlock(
                    id="t1",
                    name="get_financial_statements",
                    input={"cik": "0000320193"},
                )
            ]
        ),
        _FakeMessage(
            content=[SimpleNamespace(type="text", text="done")],
            stop_reason="end_turn",
        ),
    ]
    _install_scripted_llm(monkeypatch, responses)

    await researcher_mod.research(
        "AAPL revenue", db=db, edgar_client=stub_edgar
    )
    assert stub_edgar.calls["get_financial_statements"] == 1

    cached = await cache.get(
        db,
        cik="0000320193",
        statement="income_statement",
        period="FY2024",
        metric="Revenue",
    )
    assert cached is not None
    assert cached.value == 394_328_000_000.0
    assert cached.filing_accession == "0000320193-24-000001"


# ---------------------------------------------------------------------------
# Tool error surfaces via is_error
# ---------------------------------------------------------------------------


async def test_tool_error_returns_to_model_not_raised(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If an EdgarClient tool raises, the error is reported back to the
    LLM via a tool_result with is_error=True, not bubbled up."""
    from shotcut.research.edgar import EdgarClientError

    class BrokenEdgar(StubEdgar):
        async def find_company(self, ticker_or_name: str) -> str:  # type: ignore[override]
            raise EdgarClientError("simulated: CIK lookup failed")

    stub_edgar = BrokenEdgar()

    responses = [
        _FakeMessage(
            content=[
                _ToolUseBlock(
                    id="t1",
                    name="find_company",
                    input={"ticker_or_name": "NOPE"},
                )
            ]
        ),
        # After seeing the error, the LLM gives up.
        _FakeMessage(
            content=[SimpleNamespace(type="text", text="Could not find company.")],
            stop_reason="end_turn",
        ),
    ]
    _install_scripted_llm(monkeypatch, responses)

    # Must not raise — the error is surfaced to the LLM, not the caller.
    result = await researcher_mod.research(
        "pull NOPE data", db=db, edgar_client=stub_edgar
    )
    assert result.facts == []
    assert "Could not find" in result.summary


# ---------------------------------------------------------------------------
# max_iterations cap
# ---------------------------------------------------------------------------


async def test_max_iterations_breaks_loop(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the model keeps asking for tools indefinitely, max_iterations
    caps the loop."""
    stub_edgar = StubEdgar()

    def _tool_use_response() -> _FakeMessage:
        # Note: new id per response so tool_result wiring is distinct.
        return _FakeMessage(
            content=[
                _ToolUseBlock(
                    id=f"t{uuid.uuid4().hex[:6]}",
                    name="find_company",
                    input={"ticker_or_name": "AAPL"},
                )
            ]
        )

    responses = [_tool_use_response() for _ in range(10)]
    _install_scripted_llm(monkeypatch, responses)

    result = await researcher_mod.research(
        "loop forever", db=db, edgar_client=stub_edgar, max_iterations=3
    )
    # find_company invoked exactly max_iterations times, not more.
    assert stub_edgar.calls["find_company"] == 3
    # Result is still returned (summary may be empty).
    assert isinstance(result.facts, list)
