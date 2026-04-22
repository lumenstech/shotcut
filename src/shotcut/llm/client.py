"""Anthropic SDK wrapper.

One shared `AsyncAnthropic` client per process. Agents call this module
rather than constructing their own clients so we can layer retries,
telemetry, and test fakes in one place.

Defaults follow the claude-api skill guidance:
  - model: claude-opus-4-7
  - thinking: adaptive
  - effort: high
  - streaming for large max_tokens

Stage 9 adds `traced_create` / `traced_parse` wrappers that emit
observability spans, Prometheus metrics, and cost attribution onto
every Anthropic call. Agents use these helpers; direct
`client.messages.create(...)` stays available for code paths that
don't want observability overhead (it's not zero even with NoOp
tracer — we still construct the span).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, TypeVar

from anthropic import AsyncAnthropic
from anthropic.types import TextBlockParam

from shotcut.config import settings
from shotcut.observability import metrics, tracing
from shotcut.observability.cost import compute_cost


@lru_cache(maxsize=1)
def get_client() -> AsyncAnthropic:
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


def cached_system(text: str) -> list[TextBlockParam]:
    """Render a system prompt as a single cacheable block.

    The minimum cacheable prefix on Opus 4.7 is 4096 tokens — short prompts
    won't actually cache, but the marker is harmless.
    """
    return [
        TextBlockParam(
            type="text",
            text=text,
            cache_control={"type": "ephemeral"},
        )
    ]


T = TypeVar("T")


async def traced_create(
    client: AsyncAnthropic, *, tenant_id: str = "", **kwargs: Any
) -> Any:
    """`client.messages.create(**kwargs)` with observability side effects.

    Opens an `llm.messages.create` span, executes the call, tags the
    span + Prometheus counters with the response's token usage and
    computed cost, and emits an agent-error counter on exception.

    `tenant_id` is a string because Prometheus labels are strings and
    we roll LLM cost up per tenant. Pass `str(user.tenant_id)` from
    callers; empty string is the anonymous / pre-auth default.
    """
    model = kwargs.get("model", "unknown")
    with tracing.start_span("llm.messages.create", model=model) as span:
        try:
            response = await client.messages.create(**kwargs)
        except Exception as exc:
            metrics.AGENT_ERRORS_TOTAL.labels(agent="llm").inc()
            raise exc
        _record_usage(span, response=response, model=model, tenant_id=tenant_id)
        return response


async def traced_parse(
    client: AsyncAnthropic, *, tenant_id: str = "", **kwargs: Any
) -> Any:
    """`client.messages.parse(**kwargs)` with observability side effects.

    Mirrors `traced_create` but for the structured-output variant —
    `response.parsed_output` is the Pydantic model; usage / cost tags
    work the same way.
    """
    model = kwargs.get("model", "unknown")
    with tracing.start_span("llm.messages.parse", model=model) as span:
        try:
            response = await client.messages.parse(**kwargs)
        except Exception:
            metrics.AGENT_ERRORS_TOTAL.labels(agent="llm").inc()
            raise
        _record_usage(span, response=response, model=model, tenant_id=tenant_id)
        return response


def _record_usage(
    span: tracing.Span,
    *,
    response: Any,
    model: str,
    tenant_id: str,
) -> None:
    """Pull token counts off the Anthropic response usage block and tag
    the span + metrics. Defensive on shape — some SDK flavors return
    `usage` as a dict, others as a typed object."""
    usage = getattr(response, "usage", None) or {}
    get = (lambda k: getattr(usage, k, 0)) if not isinstance(usage, dict) else usage.get
    input_tokens = int(get("input_tokens") or 0)
    output_tokens = int(get("output_tokens") or 0)
    cached_read = int(get("cache_read_input_tokens") or 0)
    cached_write = int(get("cache_creation_input_tokens") or 0)

    cost = compute_cost(
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_read_tokens=cached_read,
        cached_write_tokens=cached_write,
    )

    span.set_attribute("input_tokens", input_tokens)
    span.set_attribute("output_tokens", output_tokens)
    span.set_attribute("cached_read_tokens", cached_read)
    span.set_attribute("cached_write_tokens", cached_write)
    span.set_attribute("cost_usd", cost)

    metrics.LLM_TOKENS_TOTAL.labels(model=model, kind="input").inc(input_tokens)
    metrics.LLM_TOKENS_TOTAL.labels(model=model, kind="output").inc(output_tokens)
    metrics.LLM_TOKENS_TOTAL.labels(model=model, kind="cached_read").inc(cached_read)
    metrics.LLM_TOKENS_TOTAL.labels(model=model, kind="cached_write").inc(cached_write)
    metrics.LLM_COST_USD_TOTAL.labels(model=model, tenant_id=tenant_id).inc(cost)
