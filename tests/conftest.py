"""Project-root pytest config.

Sets test-time environment variables before any test module imports
`shotcut.config` (which requires ANTHROPIC_API_KEY at construction time).
Actual Anthropic calls in tests are either stubbed or marked integration
and gated on a real key.

Shared async-db and storage fixtures for integration tests live here too.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
# Tests never talk to Auth0. AUTH_DISABLED=true makes get_current_user
# return UserContext.anonymous() without touching the verifier, so tests
# don't need to mint real JWTs unless they're specifically testing auth.
os.environ.setdefault("AUTH_DISABLED", "true")

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture
def temp_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the storage module's root to a per-test temp dir.

    The storage singleton is lru_cached, so we also clear the cache
    before and after to prevent leakage between tests.
    """
    from shotcut import storage as storage_mod
    from shotcut.config import settings

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    storage_mod.get_storage.cache_clear()
    yield tmp_path
    storage_mod.get_storage.cache_clear()


@pytest_asyncio.fixture
async def test_db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    """Async SQLAlchemy session backed by a per-test SQLite file.

    Schema is created via `Base.metadata.create_all` — the Alembic
    migration lives in `alembic/versions/` and runs against real Postgres
    in production. SQLite's SQLAlchemy layer emits a VARCHAR with a CHECK
    constraint for `sa.Enum`, which is fine for our test semantics.
    """
    from shotcut.db.models import Base

    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()
