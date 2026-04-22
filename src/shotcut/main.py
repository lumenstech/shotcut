import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from shotcut.api.routes import router
from shotcut.config import settings
from shotcut.db.models import Base
from shotcut.db.session import engine

logging.basicConfig(level=settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # MVP: auto-create tables. Productionize with Alembic migrations.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(title="Shotcut", version="0.1.0", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
