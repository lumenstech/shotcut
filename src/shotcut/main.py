import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Response

from shotcut.api.routes import router
from shotcut.config import settings
from shotcut.db import session as db_session
from shotcut.db.models import Base
from shotcut.db.session import engine
from shotcut.observability import metrics
from shotcut.observability.http import metrics_middleware
from shotcut.orchestrator import durable as durable_mod

logging.basicConfig(level=settings.log_level)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # MVP: auto-create tables. In production, `alembic upgrade head`
    # runs before the app boots and `create_all` is a no-op.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Stage 6: resume non-terminal orchestrator runs. On a multi-worker
    # deployment each worker would race here; documented as a follow-up
    # in docs/decisions/0003-durable-execution.md.
    async with db_session.SessionLocal() as db:
        try:
            resumed = await durable_mod.resume_all(db)
        except Exception:  # noqa: BLE001 — don't block startup on resume failure
            log.exception("startup: resume_all failed")
            resumed = []
    if resumed:
        log.info("startup: resumed %d non-terminal session(s)", len(resumed))
    yield


app = FastAPI(title="Shotcut", version="0.1.0", lifespan=lifespan)

# Stage 9: HTTP metrics middleware. Registered before the router so
# every request (including auth failures) is counted.
app.middleware("http")(metrics_middleware)

app.include_router(router)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/metrics")
async def prometheus_metrics() -> Response:
    """Prometheus text-format exposition.

    Intentionally unauthenticated — Prometheus scrapers operate inside a
    network boundary and expect to scrape without bearer tokens. Mount
    behind firewall rules in production.
    """
    return Response(
        content=metrics.prometheus_exposition(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
