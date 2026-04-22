import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from shotcut.api.routes import router
from shotcut.config import settings
from shotcut.db.models import Base
from shotcut.db import session as db_session
from shotcut.db.session import engine
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
app.include_router(router)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok"}
