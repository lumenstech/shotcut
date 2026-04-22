"""HTTP-level tests for the approve/reject endpoints.

Exercises the full request/response cycle via FastAPI's TestClient:
- POST /sessions/{id}/actions/{id}/approve flips status to applied
- POST /sessions/{id}/actions/{id}/reject flips status to rejected
- Rejecting an already-applied action returns 409
- Approving a nonexistent action returns 404
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook as OpenpyxlWorkbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shotcut.agents import executor, planner, verifier
from shotcut.agents.planner import Plan, PlanStep
from shotcut.agents.verifier import VerificationReport
from shotcut.api.routes import router
from shotcut.db.models import Action, ActionStatus, Base, Session as SessionRow
from shotcut.db.session import get_db
from shotcut.spreadsheet.actions import Action as AgentAction
from shotcut.spreadsheet.actions import WriteValue


@pytest_asyncio.fixture
async def app_with_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """FastAPI app wired to a per-test SQLite + isolated storage.

    Yields (app, session_factory, session_row) — the caller uses
    session_factory to inspect audit rows and TestClient(app) for HTTP.
    """
    from shotcut import storage as storage_mod
    from shotcut.config import settings

    # Storage redirect.
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    storage_mod.get_storage.cache_clear()

    # Per-test SQLite.
    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Seed a session with an uploaded original workbook containing user data.
    session_id = uuid.uuid4()
    session_dir = tmp_path / "workbooks" / str(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    original_path = session_dir / "original.xlsx"

    pyxl = OpenpyxlWorkbook()
    pyxl.active.title = "Sheet1"
    for i in range(1, 11):
        pyxl.active[f"A{i}"] = i * 100
    pyxl.save(original_path)

    async with session_factory() as db:
        row = SessionRow(
            id=session_id,
            workbook_path=str(original_path),
            original_workbook_path=str(original_path),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

    # Build app with dependency override.
    app = FastAPI()
    app.include_router(router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db

    # Patch planner / executor / verifier so the prompt endpoint is fast and deterministic.
    async def fake_plan(prompt: str, workbook_summary: dict) -> Plan:
        return Plan(summary="test", steps=[PlanStep(title="write", description="")])

    async def fake_execute(
        step: PlanStep, workbook_summary: dict, max_iterations: int = 6
    ) -> list[AgentAction]:
        # Target A5 — always conflicts with the seeded user data.
        return [WriteValue(sheet="Sheet1", target="A5", value=9999)]

    async def fake_verify_semantics(prompt: str, workbook: object) -> VerificationReport:
        return VerificationReport(issues=[], confidence=1.0)

    monkeypatch.setattr(planner, "plan", fake_plan)
    monkeypatch.setattr(executor, "execute", fake_execute)
    monkeypatch.setattr(verifier, "verify_semantics", fake_verify_semantics)

    try:
        yield app, session_factory, session_id
    finally:
        await engine.dispose()
        storage_mod.get_storage.cache_clear()


async def _get_pending_action_id(session_factory, session_id: uuid.UUID) -> uuid.UUID:
    async with session_factory() as db:
        stmt = select(Action).where(Action.session_id == session_id)
        row = (await db.execute(stmt)).scalar_one()
        assert row.status is ActionStatus.PENDING_APPROVAL
        return row.id


async def test_approve_endpoint_applies_action(app_with_db) -> None:
    app, session_factory, session_id = app_with_db
    client = TestClient(app)

    # Run prompt to generate a pending action.
    response = client.post(
        f"/sessions/{session_id}/prompt",
        json={"prompt": "write A5"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["actions_applied"] == 0
    assert len(body["pending_approvals"]) == 1
    action_id = body["pending_approvals"][0]["action_id"]

    # Approve.
    approve_response = client.post(
        f"/sessions/{session_id}/actions/{action_id}/approve"
    )
    assert approve_response.status_code == 200, approve_response.text
    assert approve_response.json()["status"] == "applied"

    # Audit row now applied with force_override=True.
    async with session_factory() as db:
        row = (
            await db.execute(select(Action).where(Action.id == uuid.UUID(action_id)))
        ).scalar_one()
        assert row.status is ActionStatus.APPLIED
        assert row.force_override is True


async def test_reject_endpoint_marks_action_rejected(app_with_db) -> None:
    app, session_factory, session_id = app_with_db
    client = TestClient(app)

    response = client.post(
        f"/sessions/{session_id}/prompt",
        json={"prompt": "write A5"},
    )
    action_id = response.json()["pending_approvals"][0]["action_id"]

    reject_response = client.post(
        f"/sessions/{session_id}/actions/{action_id}/reject"
    )
    assert reject_response.status_code == 200
    assert reject_response.json()["status"] == "rejected"

    async with session_factory() as db:
        row = (
            await db.execute(select(Action).where(Action.id == uuid.UUID(action_id)))
        ).scalar_one()
        assert row.status is ActionStatus.REJECTED
        assert row.force_override is False


async def test_approve_nonexistent_action_returns_404(app_with_db) -> None:
    app, _, session_id = app_with_db
    client = TestClient(app)

    bogus = uuid.uuid4()
    response = client.post(f"/sessions/{session_id}/actions/{bogus}/approve")
    assert response.status_code == 404


async def test_approve_already_applied_returns_409(app_with_db) -> None:
    app, session_factory, session_id = app_with_db
    client = TestClient(app)

    # First prompt: pending.
    prompt_response = client.post(
        f"/sessions/{session_id}/prompt",
        json={"prompt": "write A5"},
    )
    action_id = prompt_response.json()["pending_approvals"][0]["action_id"]

    # Approve (→ applied).
    client.post(f"/sessions/{session_id}/actions/{action_id}/approve")

    # Approve again: 409 because status is no longer pending_approval.
    second = client.post(f"/sessions/{session_id}/actions/{action_id}/approve")
    assert second.status_code == 409
