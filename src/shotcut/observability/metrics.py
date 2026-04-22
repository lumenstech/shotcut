"""Prometheus metrics for Stage 9 observability.

Metric set per docs/decisions/0006-observability.md §Metrics. All
metrics live in a single module-level `CollectorRegistry` so tests can
snapshot values without interfering with the process-wide default
registry (and vice versa — `/metrics` serving uses this registry, not
the default).
"""
from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

# Single registry for the process. Tests can also inspect this one
# directly; reset between tests via `reset_metrics()` below.
REGISTRY = CollectorRegistry()


# --- HTTP ---------------------------------------------------------------

REQUESTS_TOTAL = Counter(
    "shotcut_requests_total",
    "Total HTTP requests handled by the shotcut backend.",
    labelnames=("method", "path", "status"),
    registry=REGISTRY,
)

REQUEST_DURATION_SECONDS = Histogram(
    "shotcut_request_duration_seconds",
    "Wall-clock duration of HTTP requests.",
    labelnames=("method", "path"),
    registry=REGISTRY,
)


# --- Agent steps --------------------------------------------------------

AGENT_STEP_DURATION_SECONDS = Histogram(
    "shotcut_agent_step_duration_seconds",
    "Wall-clock duration of a single agent step.",
    labelnames=("agent",),
    registry=REGISTRY,
)

AGENT_ERRORS_TOTAL = Counter(
    "shotcut_agent_errors_total",
    "Errors surfaced from agent execution.",
    labelnames=("agent",),
    registry=REGISTRY,
)


# --- LLM ----------------------------------------------------------------

LLM_TOKENS_TOTAL = Counter(
    "shotcut_llm_tokens_total",
    "Tokens consumed by Anthropic calls, split by kind.",
    labelnames=("model", "kind"),  # kind: input | output | cached_read | cached_write
    registry=REGISTRY,
)

LLM_COST_USD_TOTAL = Counter(
    "shotcut_llm_cost_usd_total",
    "USD cost of Anthropic calls, rolled up per tenant + model.",
    labelnames=("model", "tenant_id"),
    registry=REGISTRY,
)


def prometheus_exposition() -> bytes:
    """Serialize the current registry snapshot in the Prometheus text
    format. Served verbatim by `GET /metrics`."""
    return generate_latest(REGISTRY)


def reset_metrics() -> None:
    """Clear every counter / histogram. Tests call this between cases
    to avoid cross-test contamination.

    We unregister and re-register the collectors rather than zeroing in
    place — `prometheus_client`'s `Counter.reset()` is not a public API.
    """
    global REGISTRY, REQUESTS_TOTAL, REQUEST_DURATION_SECONDS
    global AGENT_STEP_DURATION_SECONDS, AGENT_ERRORS_TOTAL
    global LLM_TOKENS_TOTAL, LLM_COST_USD_TOTAL

    REGISTRY = CollectorRegistry()
    REQUESTS_TOTAL = Counter(
        "shotcut_requests_total",
        "Total HTTP requests handled by the shotcut backend.",
        labelnames=("method", "path", "status"),
        registry=REGISTRY,
    )
    REQUEST_DURATION_SECONDS = Histogram(
        "shotcut_request_duration_seconds",
        "Wall-clock duration of HTTP requests.",
        labelnames=("method", "path"),
        registry=REGISTRY,
    )
    AGENT_STEP_DURATION_SECONDS = Histogram(
        "shotcut_agent_step_duration_seconds",
        "Wall-clock duration of a single agent step.",
        labelnames=("agent",),
        registry=REGISTRY,
    )
    AGENT_ERRORS_TOTAL = Counter(
        "shotcut_agent_errors_total",
        "Errors surfaced from agent execution.",
        labelnames=("agent",),
        registry=REGISTRY,
    )
    LLM_TOKENS_TOTAL = Counter(
        "shotcut_llm_tokens_total",
        "Tokens consumed by Anthropic calls, split by kind.",
        labelnames=("model", "kind"),
        registry=REGISTRY,
    )
    LLM_COST_USD_TOTAL = Counter(
        "shotcut_llm_cost_usd_total",
        "USD cost of Anthropic calls, rolled up per tenant + model.",
        labelnames=("model", "tenant_id"),
        registry=REGISTRY,
    )
