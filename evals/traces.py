"""Stage 10 observability glue: tags every eval invocation with
`trigger=eval`, `eval_suite`, and `eval_case_id` so production traces
and eval traces filter cleanly in Langfuse.

`EvalContext` is the one-and-only entry point — wrap the runner's
per-case logic in `async with EvalContext(...)`. The context:
  - opens a `session.turn` span with the eval-taxonomy attributes,
  - exposes `trace_id` for the result-row's `langfuse_trace_id` field
    (under the NoOp tracer this is a locally-generated UUID; under
    Langfuse, the span's concrete trace id),
  - re-raises any exception from the wrapped block so the runner
    records a failed verdict.
"""
from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Literal

from shotcut.observability import tracing

Suite = Literal["golden", "spreadsheetbench"]


class EvalContext(AbstractAsyncContextManager["EvalContext"]):
    """One-per-case tracing scope. Opens a root span tagged for eval
    filtering, exposes its id for the result-row.
    """

    def __init__(self, *, suite: Suite, case_id: str) -> None:
        self.suite = suite
        self.case_id = case_id
        self._span_cm: AbstractAsyncContextManager[object] | None = None
        self.trace_id: uuid.UUID = uuid.uuid4()

    async def __aenter__(self) -> "EvalContext":
        # contextmanager `tracing.start_span` is sync; we invoke it
        # here so the span's lifetime tracks the `async with`.
        self._span_cm = _AsyncSpanWrapper(
            tracing.start_span(
                "session.turn",
                trigger="eval",
                eval_suite=self.suite,
                eval_case_id=self.case_id,
                eval_trace_id=str(self.trace_id),
            )
        )
        await self._span_cm.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._span_cm is not None:
            await self._span_cm.__aexit__(exc_type, exc, tb)
            self._span_cm = None


class _AsyncSpanWrapper(AbstractAsyncContextManager[object]):
    """Adapt the sync `tracing.start_span` context manager to async.

    `start_span` is a sync contextmanager; EvalContext is async so
    callers get `async with`. The wrapping is mechanical — no await
    points, just bridging the protocol.
    """

    def __init__(self, sync_cm: object) -> None:
        self._sync_cm = sync_cm
        self._entered = False

    async def __aenter__(self) -> object:
        result = self._sync_cm.__enter__()  # type: ignore[attr-defined]
        self._entered = True
        return result

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._entered:
            self._sync_cm.__exit__(exc_type, exc, tb)  # type: ignore[attr-defined]
            self._entered = False
