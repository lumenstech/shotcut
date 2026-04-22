"""Stage 9 observability: tracing, metrics, cost.

Public surface:

    from shotcut.observability import start_span, compute_cost, metrics

Individual modules import explicitly when they need concrete classes
(e.g. `RecordingTracer` for tests).
"""
from shotcut.observability import cost, metrics, tracing
from shotcut.observability.tracing import Span, Tracer, start_span

__all__ = ["Span", "Tracer", "cost", "metrics", "start_span", "tracing"]
