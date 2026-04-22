"""traced_create + traced_parse: spans emitted, metrics incremented.

Uses a fake Anthropic client that returns a response with a usage
block matching the SDK's shape. Validates:

- `llm.messages.create` / `llm.messages.parse` spans are created.
- Token counts + cost_usd land on span attributes.
- Prometheus counters (`shotcut_llm_tokens_total`,
  `shotcut_llm_cost_usd_total`) advance by the right amounts.
- Errors from the underlying client record on the span and increment
  `shotcut_agent_errors_total{agent="llm"}`.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from shotcut.llm.client import traced_create, traced_parse
from shotcut.observability import metrics
from shotcut.observability.tracing import (
    RecordingTracer,
    reset_tracer,
    set_tracer,
)


class _FakeUsage:
    def __init__(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class _FakeMessages:
    def __init__(self, *, response: Any = None, raises: Exception | None = None) -> None:
        self._response = response
        self._raises = raises

    async def create(self, **_kwargs: Any) -> Any:
        if self._raises is not None:
            raise self._raises
        return self._response

    async def parse(self, **_kwargs: Any) -> Any:
        if self._raises is not None:
            raise self._raises
        return self._response


class _FakeClient:
    def __init__(
        self, *, response: Any = None, raises: Exception | None = None
    ) -> None:
        self.messages = _FakeMessages(response=response, raises=raises)


@pytest.fixture(autouse=True)
def _install_recorder():
    recorder = RecordingTracer()
    set_tracer(recorder)
    metrics.reset_metrics()
    yield recorder
    reset_tracer()
    metrics.reset_metrics()


async def test_traced_create_emits_span_with_token_attrs(
    _install_recorder: RecordingTracer,
) -> None:
    response = SimpleNamespace(
        usage=_FakeUsage(
            input_tokens=1234,
            output_tokens=567,
            cache_read_input_tokens=89,
            cache_creation_input_tokens=10,
        )
    )
    client = _FakeClient(response=response)

    result = await traced_create(client, model="claude-opus-4-7", tenant_id="t-1")
    assert result is response

    spans = [s for s in _install_recorder.spans if s.name == "llm.messages.create"]
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["model"] == "claude-opus-4-7"
    assert span.attributes["input_tokens"] == 1234
    assert span.attributes["output_tokens"] == 567
    assert span.attributes["cached_read_tokens"] == 89
    assert span.attributes["cached_write_tokens"] == 10
    # Cost > 0 for non-zero tokens.
    assert span.attributes["cost_usd"] > 0


async def test_traced_create_increments_prometheus_counters(
    _install_recorder: RecordingTracer,
) -> None:
    response = SimpleNamespace(
        usage=_FakeUsage(input_tokens=1000, output_tokens=250)
    )
    client = _FakeClient(response=response)

    await traced_create(client, model="claude-opus-4-7", tenant_id="t-1")

    # Token counter should show 1000 input + 250 output for opus-4-7.
    text = metrics.prometheus_exposition().decode()
    assert 'shotcut_llm_tokens_total{kind="input",model="claude-opus-4-7"} 1000.0' in text
    assert 'shotcut_llm_tokens_total{kind="output",model="claude-opus-4-7"} 250.0' in text
    # Cost counter labeled by tenant.
    assert 'shotcut_llm_cost_usd_total{model="claude-opus-4-7",tenant_id="t-1"}' in text


async def test_traced_parse_also_emits_span(
    _install_recorder: RecordingTracer,
) -> None:
    response = SimpleNamespace(
        usage=_FakeUsage(input_tokens=500, output_tokens=100),
        parsed_output=None,
    )
    client = _FakeClient(response=response)

    await traced_parse(client, model="claude-sonnet-4-6")

    spans = [s for s in _install_recorder.spans if s.name == "llm.messages.parse"]
    assert len(spans) == 1
    assert spans[0].attributes["model"] == "claude-sonnet-4-6"
    assert spans[0].attributes["input_tokens"] == 500


async def test_traced_create_records_exception(
    _install_recorder: RecordingTracer,
) -> None:
    client = _FakeClient(raises=RuntimeError("API down"))
    with pytest.raises(RuntimeError, match="API down"):
        await traced_create(client, model="claude-opus-4-7")

    spans = [s for s in _install_recorder.spans if s.name == "llm.messages.create"]
    assert len(spans) == 1
    assert isinstance(spans[0].exception, RuntimeError)

    # Agent-error counter bumps for the LLM agent label.
    text = metrics.prometheus_exposition().decode()
    assert 'shotcut_agent_errors_total{agent="llm"} 1.0' in text


async def test_usage_as_dict_also_works(_install_recorder: RecordingTracer) -> None:
    """Some SDK variants return `usage` as a dict instead of a typed object.
    Defensive extraction in `_record_usage` handles both."""
    response = SimpleNamespace(
        usage={"input_tokens": 42, "output_tokens": 8}
    )
    client = _FakeClient(response=response)
    await traced_create(client, model="claude-haiku-4-5")

    span = next(s for s in _install_recorder.spans if s.name == "llm.messages.create")
    assert span.attributes["input_tokens"] == 42
    assert span.attributes["output_tokens"] == 8
