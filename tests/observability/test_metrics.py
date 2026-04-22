"""Prometheus metrics: counter/histogram behavior + exposition format."""
from __future__ import annotations

import pytest
from prometheus_client.parser import text_string_to_metric_families

from shotcut.observability import metrics


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset_metrics()
    yield
    metrics.reset_metrics()


def test_requests_total_counter_increments() -> None:
    metrics.REQUESTS_TOTAL.labels(method="GET", path="/x", status="200").inc()
    metrics.REQUESTS_TOTAL.labels(method="GET", path="/x", status="200").inc()
    metrics.REQUESTS_TOTAL.labels(method="POST", path="/y", status="400").inc()

    text = metrics.prometheus_exposition().decode()
    families = {fam.name: fam for fam in text_string_to_metric_families(text)}
    samples = families["shotcut_requests"].samples
    by_labels = {
        (s.labels["method"], s.labels["path"], s.labels["status"]): s.value
        for s in samples
        if s.name == "shotcut_requests_total"
    }
    assert by_labels[("GET", "/x", "200")] == 2.0
    assert by_labels[("POST", "/y", "400")] == 1.0


def test_llm_tokens_per_model_per_kind() -> None:
    metrics.LLM_TOKENS_TOTAL.labels(
        model="claude-opus-4-7", kind="input"
    ).inc(1000)
    metrics.LLM_TOKENS_TOTAL.labels(
        model="claude-opus-4-7", kind="output"
    ).inc(250)
    metrics.LLM_TOKENS_TOTAL.labels(
        model="claude-haiku-4-5", kind="input"
    ).inc(500)

    text = metrics.prometheus_exposition().decode()
    families = {fam.name: fam for fam in text_string_to_metric_families(text)}
    samples = families["shotcut_llm_tokens"].samples
    assert any(
        s.labels["model"] == "claude-opus-4-7"
        and s.labels["kind"] == "input"
        and s.value == 1000.0
        for s in samples
    )
    assert any(
        s.labels["model"] == "claude-haiku-4-5"
        and s.labels["kind"] == "input"
        and s.value == 500.0
        for s in samples
    )


def test_cost_counter_rolls_up_per_tenant() -> None:
    tenant_a = "11111111-1111-1111-1111-111111111111"
    tenant_b = "22222222-2222-2222-2222-222222222222"
    metrics.LLM_COST_USD_TOTAL.labels(
        model="claude-opus-4-7", tenant_id=tenant_a
    ).inc(0.01)
    metrics.LLM_COST_USD_TOTAL.labels(
        model="claude-opus-4-7", tenant_id=tenant_a
    ).inc(0.02)
    metrics.LLM_COST_USD_TOTAL.labels(
        model="claude-opus-4-7", tenant_id=tenant_b
    ).inc(0.05)

    text = metrics.prometheus_exposition().decode()
    families = {fam.name: fam for fam in text_string_to_metric_families(text)}
    samples = families["shotcut_llm_cost_usd"].samples
    totals: dict[str, float] = {}
    for s in samples:
        if s.name == "shotcut_llm_cost_usd_total":
            totals[s.labels["tenant_id"]] = s.value
    assert totals[tenant_a] == pytest.approx(0.03)
    assert totals[tenant_b] == pytest.approx(0.05)


def test_agent_errors_counter() -> None:
    metrics.AGENT_ERRORS_TOTAL.labels(agent="planner").inc()
    metrics.AGENT_ERRORS_TOTAL.labels(agent="executor").inc()
    metrics.AGENT_ERRORS_TOTAL.labels(agent="executor").inc()

    text = metrics.prometheus_exposition().decode()
    families = {fam.name: fam for fam in text_string_to_metric_families(text)}
    samples = families["shotcut_agent_errors"].samples
    totals = {
        s.labels["agent"]: s.value
        for s in samples
        if s.name == "shotcut_agent_errors_total"
    }
    assert totals["planner"] == 1.0
    assert totals["executor"] == 2.0


def test_histogram_observations_produce_bucket_samples() -> None:
    metrics.AGENT_STEP_DURATION_SECONDS.labels(agent="planner").observe(0.1)
    metrics.AGENT_STEP_DURATION_SECONDS.labels(agent="planner").observe(0.5)
    metrics.AGENT_STEP_DURATION_SECONDS.labels(agent="planner").observe(2.5)

    text = metrics.prometheus_exposition().decode()
    families = {fam.name: fam for fam in text_string_to_metric_families(text)}
    samples = families["shotcut_agent_step_duration_seconds"].samples
    count_samples = [
        s for s in samples
        if s.name == "shotcut_agent_step_duration_seconds_count"
    ]
    assert any(s.labels["agent"] == "planner" and s.value == 3.0 for s in count_samples)


def test_reset_metrics_zeroes_everything() -> None:
    metrics.REQUESTS_TOTAL.labels(method="GET", path="/x", status="200").inc()
    metrics.reset_metrics()
    text = metrics.prometheus_exposition().decode()
    # No counter values for the labels we set earlier.
    assert "GET" not in text or "/x" not in text
