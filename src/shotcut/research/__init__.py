"""Stage 7 research package — SEC EDGAR integration + fact cache.

Re-exports so callers write `from shotcut.research import edgar, cache,
rate_limit, facts` uniformly. See
docs/decisions/0004-research-integration.md.
"""
from shotcut.research import cache, edgar, facts, rate_limit

__all__ = ["cache", "edgar", "facts", "rate_limit"]
