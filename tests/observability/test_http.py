"""HTTP layer: metrics middleware + /metrics endpoint.

Validates the Stage 9 acceptance "every API request produces a trace /
counter increment" through the actual FastAPI surface.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from shotcut.observability import metrics
from shotcut.observability.http import metrics_middleware


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset_metrics()
    yield
    metrics.reset_metrics()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(metrics_middleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("bang")

    return app


def test_ok_request_counted() -> None:
    client = TestClient(_build_app())
    client.get("/health")
    client.get("/health")

    text = metrics.prometheus_exposition().decode()
    assert 'shotcut_requests_total{method="GET",path="/health",status="200"} 2.0' in text


def test_path_template_used_not_raw_url() -> None:
    """Histogram label is the route template so cardinality stays bounded."""
    client = _build_app()
    test = TestClient(client)
    test.get("/items/abc")
    test.get("/items/xyz")

    text = metrics.prometheus_exposition().decode()
    # Two calls to the template '/items/{item_id}', not two templates
    # keyed on raw values.
    assert 'path="/items/{item_id}"' in text
    assert 'path="/items/abc"' not in text


def test_exception_counts_as_500() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 500

    text = metrics.prometheus_exposition().decode()
    # Either the middleware recorded 500 directly (if it saw the raise)
    # or the downstream error handler produced a 500 response the
    # middleware observed — either way, a 500 row for /boom.
    assert 'path="/boom"' in text
    assert 'status="500"' in text


def test_request_duration_histogram_populates() -> None:
    client = TestClient(_build_app())
    client.get("/health")

    text = metrics.prometheus_exposition().decode()
    families = {fam.name: fam for fam in text_string_to_metric_families(text)}
    count_samples = [
        s for s in families["shotcut_request_duration_seconds"].samples
        if s.name == "shotcut_request_duration_seconds_count"
    ]
    assert any(
        s.labels["method"] == "GET"
        and s.labels["path"] == "/health"
        and s.value >= 1.0
        for s in count_samples
    )


def test_metrics_endpoint_serves_exposition_format() -> None:
    """The actual production app exposes /metrics unauthenticated."""
    from shotcut.main import app as production_app

    client = TestClient(production_app)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    # At least one known metric family shows up in the text.
    assert "shotcut_requests_total" in response.text
