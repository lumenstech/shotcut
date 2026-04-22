"""Tracing primitives with pluggable backends.

`start_span(name, **attrs)` is the single entry point: a context
manager (sync+async safe) that creates a span, binds it as the current
parent in a `ContextVar`, yields it, and ends it on exit. Nested
`start_span` calls automatically inherit the current parent without
the caller threading references.

Three backends:

- `NoOpTracer` — default. Spans are cheap no-ops; zero overhead, zero
  side effects, no external network.
- `RecordingTracer` — tests. Collects every span into an in-memory
  list so assertions can inspect the tree.
- `LangfuseTracer` — production. Lazy-imports the `langfuse` SDK so
  dev/test environments that pick `noop` don't need it installed.

See docs/decisions/0006-observability.md for the taxonomy + design.
"""
from __future__ import annotations

import abc
import contextvars
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# ContextVar scoped to the current async task / coroutine frame. Inherits
# through `await` so orchestrator → agent → LLM call nesting works
# without passing span handles around.
_current_span: contextvars.ContextVar["Span | None"] = contextvars.ContextVar(
    "shotcut_current_span", default=None
)


class Span(abc.ABC):
    """Opaque span handle. Attributes are free-form; the taxonomy
    (docs/decisions/0006-observability.md) names the keys that
    downstream consumers rely on."""

    @abc.abstractmethod
    def set_attribute(self, key: str, value: Any) -> None: ...

    @abc.abstractmethod
    def record_exception(self, exc: BaseException) -> None: ...

    @abc.abstractmethod
    def end(self) -> None: ...


class Tracer(abc.ABC):
    """Backend-agnostic tracer. `start_span()` below is the public
    caller entry point; backends don't need to handle parent inheritance —
    the context-manager wrapper takes care of that."""

    @abc.abstractmethod
    def start(self, name: str, *, parent: Span | None, attributes: dict[str, Any]) -> Span: ...


# ---------------------------------------------------------------------------
# NoOp backend (default)
# ---------------------------------------------------------------------------


class NoOpSpan(Span):
    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def record_exception(self, exc: BaseException) -> None:
        pass

    def end(self) -> None:
        pass


class NoOpTracer(Tracer):
    def start(
        self, name: str, *, parent: Span | None, attributes: dict[str, Any]
    ) -> Span:
        return NoOpSpan()


# ---------------------------------------------------------------------------
# Recording backend (tests)
# ---------------------------------------------------------------------------


@dataclass
class RecordedSpan(Span):
    """In-memory span used by `RecordingTracer`.

    Carries enough metadata to let tests assert on tree shape: `name`,
    `parent_id`, `attributes`, start/end times, and any recorded
    exception.
    """

    name: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    parent_id: uuid.UUID | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    exception: BaseException | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def record_exception(self, exc: BaseException) -> None:
        self.exception = exc

    def end(self) -> None:
        self.ended_at = time.monotonic()


class RecordingTracer(Tracer):
    """Collects every span into `spans`. Tests read the list after
    exercising the code under test."""

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []

    def start(
        self, name: str, *, parent: Span | None, attributes: dict[str, Any]
    ) -> RecordedSpan:
        parent_id = None
        if isinstance(parent, RecordedSpan):
            parent_id = parent.id
        span = RecordedSpan(
            name=name,
            parent_id=parent_id,
            attributes=dict(attributes),
        )
        self.spans.append(span)
        return span

    def clear(self) -> None:
        self.spans.clear()


# ---------------------------------------------------------------------------
# Langfuse backend (production)
# ---------------------------------------------------------------------------


class _LangfuseSpan(Span):
    """Thin adapter around a langfuse span object.

    langfuse's Python SDK exposes `span.update(...)`, `span.end()`, and
    `span.record_exception(...)`. We normalize onto our ABC.
    """

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    def set_attribute(self, key: str, value: Any) -> None:
        try:
            self._handle.update(metadata={key: value})
        except Exception:  # noqa: BLE001
            log.debug("langfuse: set_attribute failed", exc_info=True)

    def record_exception(self, exc: BaseException) -> None:
        try:
            self._handle.update(level="ERROR", status_message=str(exc))
        except Exception:  # noqa: BLE001
            log.debug("langfuse: record_exception failed", exc_info=True)

    def end(self) -> None:
        try:
            self._handle.end()
        except Exception:  # noqa: BLE001
            log.debug("langfuse: end failed", exc_info=True)


class LangfuseTracer(Tracer):
    """Wraps the `langfuse` SDK. Lazy-imports so environments that pick
    `noop` don't need langfuse installed."""

    def __init__(
        self, *, host: str, public_key: str, secret_key: str
    ) -> None:
        try:
            from langfuse import Langfuse  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "LangfuseTracer requires the `langfuse` package; install "
                "it or switch TRACING_BACKEND to 'noop'"
            ) from exc
        self._client = Langfuse(
            host=host,
            public_key=public_key,
            secret_key=secret_key,
        )

    def start(
        self, name: str, *, parent: Span | None, attributes: dict[str, Any]
    ) -> Span:
        try:
            if isinstance(parent, _LangfuseSpan):
                handle = parent._handle.span(name=name, metadata=attributes)  # noqa: SLF001
            else:
                handle = self._client.trace(name=name, metadata=attributes)
            return _LangfuseSpan(handle)
        except Exception:  # noqa: BLE001
            log.debug("langfuse: start failed; falling back to NoOp", exc_info=True)
            return NoOpSpan()


# ---------------------------------------------------------------------------
# Tracer resolution + public API
# ---------------------------------------------------------------------------


_tracer: Tracer | None = None


def get_tracer() -> Tracer:
    """Process-wide tracer resolved from `settings.tracing_backend` on
    first call."""
    global _tracer
    if _tracer is not None:
        return _tracer
    from shotcut.config import settings

    backend = settings.tracing_backend.lower()
    if backend == "noop":
        _tracer = NoOpTracer()
    elif backend == "langfuse":
        _tracer = LangfuseTracer(
            host=settings.langfuse_host,
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
        )
    else:
        raise ValueError(f"unknown TRACING_BACKEND: {settings.tracing_backend!r}")
    return _tracer


def set_tracer(tracer: Tracer) -> None:
    """Test-only: install a specific tracer instance. Production resolves
    from config via `get_tracer()`."""
    global _tracer
    _tracer = tracer


def reset_tracer() -> None:
    """Clear the cached tracer so the next `get_tracer()` re-reads
    settings. Used by tests that flip `tracing_backend`."""
    global _tracer
    _tracer = None


@contextmanager
def start_span(name: str, **attributes: Any) -> Iterator[Span]:
    """Start a span whose parent is the current contextvar-bound span.

    Usage:

        with start_span("agent.planner", step_title="build DCF"):
            ...

    Nested calls automatically chain — no need to pass parent handles
    around. Exceptions raised inside the `with` block are recorded on
    the span and re-raised.
    """
    tracer = get_tracer()
    parent = _current_span.get()
    span = tracer.start(name, parent=parent, attributes=attributes)
    token = _current_span.set(span)
    try:
        yield span
    except BaseException as exc:
        span.record_exception(exc)
        raise
    finally:
        span.end()
        _current_span.reset(token)


def current_span() -> Span | None:
    """The span currently on the context stack, if any."""
    return _current_span.get()
