"""HTTP middleware emitting Prometheus request metrics.

Increments `shotcut_requests_total{method,path,status}` and observes
`shotcut_request_duration_seconds{method,path}` for every request
handled by the FastAPI app. Registered in `shotcut.main` alongside
the lifespan/startup hooks.

`path` is the *matched route template* (e.g. `/sessions/{session_id}`)
not the raw URL — low cardinality and useful for per-endpoint
histograms. For unmatched paths (404s) we use the literal path.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from shotcut.observability import metrics


async def metrics_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """ASGI middleware: wrap one request in timing + status emission."""
    start = time.monotonic()
    try:
        response = await call_next(request)
        status = str(response.status_code)
    except Exception:
        # Route raised; metrics record a synthetic 500 and re-raise so
        # FastAPI's exception handlers kick in.
        metrics.REQUESTS_TOTAL.labels(
            method=request.method,
            path=_matched_path(request),
            status="500",
        ).inc()
        metrics.REQUEST_DURATION_SECONDS.labels(
            method=request.method, path=_matched_path(request)
        ).observe(time.monotonic() - start)
        raise

    duration = time.monotonic() - start
    path = _matched_path(request)
    metrics.REQUESTS_TOTAL.labels(
        method=request.method, path=path, status=status
    ).inc()
    metrics.REQUEST_DURATION_SECONDS.labels(
        method=request.method, path=path
    ).observe(duration)
    return response


def _matched_path(request: Request) -> str:
    """The low-cardinality route template if available, else the raw path."""
    route = request.scope.get("route")
    if route is not None:
        path = getattr(route, "path", None)
        if path is not None:
            return str(path)
    return request.url.path
