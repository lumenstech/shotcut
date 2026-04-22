"""Stage 8 acceptance: tenant A cannot read tenant B's sessions.

Exercises the application-layer tenant filter (RLS is production-only;
SQLite has no policy support). Two tenants, two sessions, each tenant's
route access sees only its own.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shotcut.api.routes import router
from shotcut.auth.middleware import get_current_user
from shotcut.auth.tenancy import UserContext
from shotcut.db.models import Base, Session as SessionRow
from shotcut.db.session import get_db


@pytest_asyncio.fixture
async def two_tenant_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Two tenants + two sessions. Returns (app, tenant_a_id, tenant_b_id,
    session_a_id, session_b_id)."""
    from shotcut import storage as storage_mod
    from shotcut.config import settings

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    storage_mod.get_storage.cache_clear()

    db_path = tmp_path / "tenants.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    session_a = uuid.uuid4()
    session_b = uuid.uuid4()

    # Seed one session per tenant.
    async with factory() as db:
        db.add_all(
            [
                SessionRow(
                    id=session_a,
                    workbook_path=str(tmp_path / f"{session_a}.xlsx"),
                    tenant_id=tenant_a,
                ),
                SessionRow(
                    id=session_b,
                    workbook_path=str(tmp_path / f"{session_b}.xlsx"),
                    tenant_id=tenant_b,
                ),
            ]
        )
        await db.commit()

    app = FastAPI()
    app.include_router(router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db

    try:
        yield app, tenant_a, tenant_b, session_a, session_b
    finally:
        await engine.dispose()
        storage_mod.get_storage.cache_clear()


def _override_user(app: FastAPI, tenant_id: uuid.UUID) -> None:
    """Inject a UserContext for this tenant into the app's auth dep."""
    def _fake_user() -> UserContext:
        return UserContext(sub=f"user-of-{tenant_id}", tenant_id=tenant_id)

    app.dependency_overrides[get_current_user] = _fake_user


async def test_tenant_a_cannot_read_tenant_b_audit(two_tenant_app) -> None:
    """The BUILD_PLAN acceptance: tenant A auth'd as self sees its own
    session; querying tenant B's session_id returns 404 — no leak via
    existence probing."""
    app, tenant_a, _tenant_b, session_a, session_b = two_tenant_app

    _override_user(app, tenant_a)
    client = TestClient(app)

    own = client.get(f"/sessions/{session_a}/audit")
    assert own.status_code == 200
    other = client.get(f"/sessions/{session_b}/audit")
    assert other.status_code == 404


async def test_tenant_b_mirror(two_tenant_app) -> None:
    app, _tenant_a, tenant_b, session_a, session_b = two_tenant_app

    _override_user(app, tenant_b)
    client = TestClient(app)

    assert client.get(f"/sessions/{session_b}/audit").status_code == 200
    assert client.get(f"/sessions/{session_a}/audit").status_code == 404


async def test_tenant_a_cannot_download_tenant_b_workbook(
    two_tenant_app,
) -> None:
    """Download endpoint is tenant-scoped too."""
    app, tenant_a, _tenant_b, _session_a, session_b = two_tenant_app

    _override_user(app, tenant_a)
    client = TestClient(app)

    response = client.get(f"/sessions/{session_b}/workbook")
    assert response.status_code == 404


async def test_tenant_a_cannot_undo_in_tenant_b(two_tenant_app) -> None:
    """Undo + approve + reject all tenant-scope via the owning session
    lookup, so cross-tenant action access is 404."""
    app, tenant_a, _tenant_b, _session_a, session_b = two_tenant_app

    _override_user(app, tenant_a)
    client = TestClient(app)

    bogus_action = uuid.uuid4()
    response = client.post(
        f"/sessions/{session_b}/actions/{bogus_action}/undo"
    )
    assert response.status_code == 404
