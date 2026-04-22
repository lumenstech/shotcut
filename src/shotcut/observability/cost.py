"""Per-model pricing + cost computation.

Public-rate table from the Anthropic cached 2026-04 pricing — Opus 4.7
$5/$25 per 1M (input/output), Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5.
Cached-read tokens cost ~0.1× base input; cached-write ~1.25×.

Unknown models fall back to Opus 4.7 rates with a warning — the
fallback is always more expensive than reality, so underestimates
don't leak into budget tracking.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPricing:
    input_per_mtok: float
    output_per_mtok: float

    @property
    def cached_read_per_mtok(self) -> float:
        # Cached reads: ~10% of base input.
        return self.input_per_mtok * 0.1

    @property
    def cached_write_per_mtok(self) -> float:
        # Cached writes: ~1.25× base input (5-minute TTL).
        return self.input_per_mtok * 1.25


# The leading entry-per-model is the canonical alias. Date-suffixed
# variants map to the same rates.
_PRICING: dict[str, ModelPricing] = {
    # Claude 4.x (cached 2026-04).
    "claude-opus-4-7":   ModelPricing(input_per_mtok=5.0,  output_per_mtok=25.0),
    "claude-opus-4-6":   ModelPricing(input_per_mtok=5.0,  output_per_mtok=25.0),
    "claude-sonnet-4-6": ModelPricing(input_per_mtok=3.0,  output_per_mtok=15.0),
    "claude-haiku-4-5":  ModelPricing(input_per_mtok=1.0,  output_per_mtok=5.0),
    # Legacy — kept so old traces aren't mispriced.
    "claude-opus-4-5":   ModelPricing(input_per_mtok=5.0,  output_per_mtok=25.0),
    "claude-opus-4-1":   ModelPricing(input_per_mtok=5.0,  output_per_mtok=25.0),
    "claude-sonnet-4-5": ModelPricing(input_per_mtok=3.0,  output_per_mtok=15.0),
    "claude-sonnet-4-0": ModelPricing(input_per_mtok=3.0,  output_per_mtok=15.0),
    "claude-opus-4-0":   ModelPricing(input_per_mtok=5.0,  output_per_mtok=25.0),
}

_FALLBACK = _PRICING["claude-opus-4-7"]


def pricing_for(model: str) -> ModelPricing:
    """Return the pricing for `model`, falling back to Opus 4.7 rates
    (the most expensive current Claude) on miss.

    On fallback, increments `shotcut_llm_unknown_model_total{model}` so
    operators can alert on it: conservative pricing means costs never
    under-report, but a silent model-string typo would produce
    plausible-but-wrong numbers. The counter makes the event queryable.
    """
    # Strip a trailing date suffix (`claude-haiku-4-5-20251001` →
    # `claude-haiku-4-5`). Suffixes are 8-digit dates or the literal
    # `-fast`; neither appears in our canonical keys.
    canonical = _strip_suffix(model)
    if canonical in _PRICING:
        return _PRICING[canonical]
    log.debug("cost: no pricing for model %r; using Opus 4.7 fallback", model)
    # Attribute access (not `from ... import`) so reset_metrics()'s
    # counter rebinding is respected — `from` creates a local name
    # that would freeze on the pre-reset Counter instance.
    from shotcut.observability import metrics

    metrics.LLM_UNKNOWN_MODEL_TOTAL.labels(model=model).inc()
    return _FALLBACK


def compute_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_read_tokens: int = 0,
    cached_write_tokens: int = 0,
) -> float:
    """USD cost of a single Anthropic API call.

    The caller supplies token counts from the response's `usage` block.
    Cached reads and writes are surfaced by Anthropic separately; pass
    them in so the cost line matches the invoice.
    """
    p = pricing_for(model)
    mtok = 1_000_000.0
    cost = (
        input_tokens * p.input_per_mtok / mtok
        + output_tokens * p.output_per_mtok / mtok
        + cached_read_tokens * p.cached_read_per_mtok / mtok
        + cached_write_tokens * p.cached_write_per_mtok / mtok
    )
    return cost


def _strip_suffix(model: str) -> str:
    # Drop everything from the last `-YYYYMMDD` onwards.
    parts = model.split("-")
    if len(parts) >= 2 and parts[-1].isdigit() and len(parts[-1]) == 8:
        return "-".join(parts[:-1])
    # `-fast`, `-1m`, etc.
    if parts[-1].lower() in {"fast"}:
        return "-".join(parts[:-1])
    return model
