"""HTTP-level tests for the approve/reject endpoints.

Exercises the full request/response cycle via FastAPI's TestClient.

Stage 6 note: the POST /prompt endpoint became async (202 + background
durable orchestrator). The approval tests below no longer go through
/prompt to produce a pending action — they seed it directly via
`audit.record(..., status=PENDING_APPROVAL)`. That keeps these tests
focused on the approve/reject HTTP surface; the end-to-end durable
flow has its own coverage in `tests/orchestrator/test_durable.py`.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook as OpenpyxlWorkbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shotcut.api.routes import router
from shotcut.db import audit
from shotcut.db.models import Action, ActionStatus, Base, Session as SessionRow
from shotcut.db.session import get_db
from shotcut.spreadsheet.actions import WriteValue
from shotcut.spreadsheet.workbook import Workbook


@pytest_asyncio.fixture
async def app_with_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """TestClient + a seeded session containing one PENDING_APPROVAL action.

    Yields (app, session_factory, session_id, pending_action_id).
    """
    from shotcut import storage as storage_mod
    from shotcut.config import settings

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    storage_mod.get_storage.cache_clear()

    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Seed the session — user workbook with A1:A10 populated so A5 is
    # user-occupied for the force-override check.
    session_id = uuid.uuid4()
    session_dir = tmp_path / "workbooks" / str(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    original_path = session_dir / "original.xlsx"

    pyxl = OpenpyxlWorkbook()
    pyxl.active.title = "Sheet1"
    for i in range(1, 11):
        pyxl.active[f"A{i}"] = i * 100
    pyxl.save(original_path)

    # Copy original → current so approval's Workbook.from_xlsx finds the
    # live state and can apply the override.
    current_path = session_dir / "current.xlsx"
    Workbook.from_xlsx(original_path).save(current_path)

    # Seed the session row + a pending action targeting A5.
    pending_id: uuid.UUID | None = None
    async with session_factory() as db:
        db.add(
            SessionRow(
                id=session_id,
                workbook_path=str(current_path),
                original_workbook_path=str(original_path),
            )
        )
        await db.commit()

        pending_action = WriteValue(sheet="Sheet1", target="A5", value=9999)
        row = await audit.record(
            db,
            session_id=session_id,
            agent="executor",
            action=pending_action,
            previous_value=None,
            reasoning="test",
            status=ActionStatus.PENDING_APPROVAL,
            approval_required_reason="would overwrite user-sourced cell Sheet1!A5",
        )
        await db.commit()
        pending_id = row.id

    app = FastAPI()
    app.include_router(router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db

    try:
        yield app, session_factory, session_id, pending_id
    finally:
        await engine.dispose()
        storage_mod.get_storage.cache_clear()


async def test_approve_endpoint_applies_action(app_with_pending) -> None:
    app, session_factory, session_id, action_id = app_with_pending
    client = TestClient(app)

    response = client.post(f"/sessions/{session_id}/actions/{action_id}/approve")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "applied"

    async with session_factory() as db:
        row = (
            await db.execute(select(Action).where(Action.id == action_id))
        ).scalar_one()
        assert row.status is ActionStatus.APPLIED
        assert row.force_override is True


async def test_reject_endpoint_marks_action_rejected(app_with_pending) -> None:
    app, session_factory, session_id, action_id = app_with_pending
    client = TestClient(app)

    response = client.post(f"/sessions/{session_id}/actions/{action_id}/reject")
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"

    async with session_factory() as db:
        row = (
            await db.execute(select(Action).where(Action.id == action_id))
        ).scalar_one()
        assert row.status is ActionStatus.REJECTED
        assert row.force_override is False


async def test_approve_nonexistent_action_returns_404(app_with_pending) -> None:
    app, _, session_id, _ = app_with_pending
    client = TestClient(app)

    bogus = uuid.uuid4()
    response = client.post(f"/sessions/{session_id}/actions/{bogus}/approve")
    assert response.status_code == 404


async def test_approve_already_applied_returns_409(app_with_pending) -> None:
    app, _, session_id, action_id = app_with_pending
    client = TestClient(app)

    # First approve: applied.
    client.post(f"/sessions/{session_id}/actions/{action_id}/approve")

    # Second approve: 409 because status is no longer pending_approval.
    second = client.post(f"/sessions/{session_id}/actions/{action_id}/approve")
    assert second.status_code == 409
