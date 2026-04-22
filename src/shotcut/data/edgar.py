"""edgartools wrapper.

MVP scope: one helper that returns the most recent 10-K/10-Q financial
statements as text for a given ticker. The researcher agent consumes this
excerpt and extracts the specific metrics a step needs.

edgartools requires an identity string (any "Name email" will do per SEC
guidelines). We set it at import time from settings.
"""
from __future__ import annotations

from shotcut.config import settings

_initialized = False


def _init() -> None:
    global _initialized
    if _initialized:
        return
    # edgartools lazy-imports heavy deps; import inside the function so the
    # module stays light when researcher isn't used.
    from edgar import set_identity

    set_identity(settings.edgar_identity)
    _initialized = True


def latest_financials_excerpt(ticker: str, max_chars: int = 20_000) -> str:
    """Return concatenated income statement + balance sheet + cash flow text
    from the most recent 10-K for `ticker`. Truncated to keep prompt costs
    bounded.
    """
    _init()
    from edgar import Company

    company = Company(ticker)
    filing = company.latest("10-K")
    if filing is None:
        return f"(no 10-K found for {ticker})"

    try:
        financials = filing.obj().financials
        sections = [
            ("Income Statement", financials.income_statement()),
            ("Balance Sheet", financials.balance_sheet()),
            ("Cash Flow", financials.cash_flow()),
        ]
    except Exception as exc:  # edgartools internal API varies by filing type
        return f"(could not parse financials for {ticker}: {exc})"

    out: list[str] = []
    for label, section in sections:
        out.append(f"### {label}\n{section}\n")
    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... (truncated)"
    return text
