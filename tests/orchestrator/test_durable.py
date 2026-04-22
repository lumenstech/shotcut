"""Stage 6 durable orchestrator tests.

Acceptance criteria from BUILD_PLAN.md:
- Kill worker mid-execution; restart; session resumes and completes.
- Frontend sees progress events in real time.
- Replaying a checkpoint produces identical actions (idempotent).

Planner / executor / verifier are monkeypatched — we test the state
machine, checkpoint lifecycle, event emission, and resume semantics,
not the LLM agents themselves.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shotcut.agents import executor, planner, verifier
from shotcut.agents.planner import Plan, PlanStep
from shotcut.agents.verifier import VerificationReport
from shotcut.db.models import (
    Action,
    ActionStatus,
    Base,
    OrchestratorState,
    OrchestratorStateEnum,
    Session as SessionRow,
)
from shotcut.orchestrator import checkpoint, durable, events
from shotcut.spreadsheet.actions import Action as AgentAction
from shotcut.spreadsheet.actions import WriteValue


# ---------------------------------------------------------------------------
# Shared fixtures — per-test sqlite + monkeypatched SessionLocal so the
# background task uses the test DB rather than the production Postgres.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sessionmaker_and_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Per-test aiosqlite engine + session factory, installed as the
    module-level SessionLocal the durable orchestrator uses."""
    from shotcut import storage as storage_mod
    from shotcut.config import settings
    from shotcut.db import session as db_session

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    storage_mod.get_storage.cache_clear()

    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    monkeypatch.setattr(db_session, "SessionLocal", session_factory)

    # Isolate event bus / task registry per test.
    events.reset_for_tests()
    durable.reset_for_tests()

    try:
        yield session_factory
    finally:
        await engine.dispose()
        storage_mod.get_storage.cache_clear()
        events.reset_for_tests()
        durable.reset_for_tests()


def _patch_agents(
    monkeypatch: pytest.MonkeyPatch,
    *,
    steps: list[PlanStep],
    actions_per_step: dict[str, list[AgentAction]],
) -> None:
    """Stub planner/executor/verifier with deterministic output."""

    async def fake_plan(
        prompt: str, workbook_summary: dict, *, tenant_id: str = ""
    ) -> Plan:
        return Plan(summary="canned", steps=steps)

    async def fake_execute(
        step: PlanStep, workbook_summary: dict, max_iterations: int = 6
    ) -> list[AgentAction]:
        return actions_per_step.get(step.title, [])

    async def fake_verify(
        workbook: object, *, prompt: str, pending_actions: object = None
    ) -> VerificationReport:
        return VerificationReport(findings=[], confidence=1.0)

    monkeypatch.setattr(planner, "plan", fake_plan)
    monkeypatch.setattr(executor, "execute", fake_execute)
    monkeypatch.setattr(verifier, "verify", fake_verify)


async def _seed_session(
    session_factory: async_sessionmaker, storage_dir: Path
) -> uuid.UUID:
    sid = uuid.uuid4()
    session_dir = storage_dir / "workbooks" / str(sid)
    session_dir.mkdir(parents=True, exist_ok=True)
    async with session_factory() as db:
        db.add(
            SessionRow(
                id=sid,
                workbook_path=str(session_dir / "current.xlsx"),
                original_workbook_path=None,
            )
        )
        await db.commit()
    return sid


# ---------------------------------------------------------------------------
# End-to-end happy path
# ---------------------------------------------------------------------------


async def test_durable_run_completes_end_to_end(
    sessionmaker_and_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_factory = sessionmaker_and_db
    sid = await _seed_session(session_factory, tmp_path)

    _patch_agents(
        monkeypatch,
        steps=[
            PlanStep(title="step_a", description=""),
            PlanStep(title="step_b", description=""),
        ],
        actions_per_step={
            "step_a": [WriteValue(sheet="Sheet", target="A1", value=1)],
            "step_b": [WriteValue(sheet="Sheet", target="A2", value=2)],
        },
    )

    async with session_factory() as db:
        await durable.start(db, session_id=sid, prompt="do things")

    await durable.wait(sid)

    # Final orchestrator state is DONE.
    async with session_factory() as db:
        state_row = await db.get(OrchestratorState, sid)
        assert state_row is not None
        assert state_row.state is OrchestratorStateEnum.DONE
        assert state_row.current_step_index == 2  # both steps completed
        assert state_row.plan is not None

        # Both actions were applied.
        stmt = select(Action).where(Action.session_id == sid).order_by(Action.sequence)
        rows = list((await db.execute(stmt)).scalars().all())
        assert len(rows) == 2
        assert all(r.status is ActionStatus.APPLIED for r in rows)


async def test_progress_events_emitted_during_run(
    sessionmaker_and_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SSE feed: subscriber sees PLANNING → EXECUTING → VERIFYING → DONE."""
    session_factory = sessionmaker_and_db
    sid = await _seed_session(session_factory, tmp_path)

    _patch_agents(
        monkeypatch,
        steps=[PlanStep(title="step", description="")],
        actions_per_step={
            "step": [WriteValue(sheet="Sheet", target="A1", value=1)],
        },
    )

    async with session_factory() as db:
        await durable.start(db, session_id=sid, prompt="do one thing")

    collected: list[OrchestratorStateEnum] = []
    async for event in events.subscribe(sid):
        collected.append(event.state)

    # Subscriber saw every state at least once, ending with DONE.
    assert OrchestratorStateEnum.PLANNING in collected
    assert OrchestratorStateEnum.EXECUTING in collected
    assert OrchestratorStateEnum.VERIFYING in collected
    assert collected[-1] is OrchestratorStateEnum.DONE

    # Cleanup: task completed.
    await durable.wait(sid)


# ---------------------------------------------------------------------------
# Resume semantics
# ---------------------------------------------------------------------------


async def test_resume_after_simulated_crash(
    sessionmaker_and_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a worker crash by seeding a session in EXECUTING state
    with a persisted plan and partial progress. Resume must pick up
    from `current_step_index` and finish without re-running completed
    steps."""
    session_factory = sessionmaker_and_db
    sid = await _seed_session(session_factory, tmp_path)

    steps = [
        PlanStep(title="step_1", description=""),
        PlanStep(title="step_2", description=""),
        PlanStep(title="step_3", description=""),
    ]
    actions = {
        "step_1": [WriteValue(sheet="Sheet", target="A1", value=1)],
        "step_2": [WriteValue(sheet="Sheet", target="A2", value=2)],
        "step_3": [WriteValue(sheet="Sheet", target="A3", value=3)],
    }
    _patch_agents(monkeypatch, steps=steps, actions_per_step=actions)

    # Simulate: crash happened after step_1 was applied and committed.
    # We seed the DB with an OrchestratorState at EXECUTING, step_index=1,
    # and a single audit row for step_1 — as if the real orchestrator had
    # just flushed that step before going down.
    stored_plan = Plan(summary="canned", steps=steps)
    async with session_factory() as db:
        db.add(
            OrchestratorState(
                session_id=sid,
                state=OrchestratorStateEnum.EXECUTING,
                prompt="crash-test",
                plan=stored_plan.model_dump(mode="json"),
                current_step_index=1,
            )
        )
        # Simulate the already-applied action for step_1.
        from shotcut.db import audit
        await audit.record(
            db,
            session_id=sid,
            agent="executor",
            action=actions["step_1"][0],
            previous_value=None,
            reasoning="step_1",
        )
        await db.commit()

    # Instrument the executor so we know which steps got re-invoked on resume.
    invoked_steps: list[str] = []
    real_fake = executor.execute

    async def tracking_execute(
        step: PlanStep, workbook_summary: dict, max_iterations: int = 6
    ) -> list[AgentAction]:
        invoked_steps.append(step.title)
        return await real_fake(step, workbook_summary, max_iterations)

    monkeypatch.setattr(executor, "execute", tracking_execute)

    await durable.resume(sid)
    await durable.wait(sid)

    # step_1 was NOT re-invoked — resume skipped it.
    assert "step_1" not in invoked_steps
    assert invoked_steps == ["step_2", "step_3"]

    # Final state is DONE; the session has 3 applied actions total.
    async with session_factory() as db:
        state_row = await db.get(OrchestratorState, sid)
        assert state_row is not None
        assert state_row.state is OrchestratorStateEnum.DONE

        stmt = select(Action).where(Action.session_id == sid).order_by(Action.sequence)
        rows = list((await db.execute(stmt)).scalars().all())
        assert len(rows) == 3


async def test_resume_all_scans_non_terminal(
    sessionmaker_and_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resume_all() finds every non-terminal session and kicks off a task."""
    session_factory = sessionmaker_and_db

    # Seed three sessions with mixed states.
    async with session_factory() as db:
        for state, idx in [
            (OrchestratorStateEnum.DONE, 0),
            (OrchestratorStateEnum.PLANNING, 1),
            (OrchestratorStateEnum.FAILED, 2),
            (OrchestratorStateEnum.VERIFYING, 3),
        ]:
            sid = uuid.uuid4()
            session_dir = tmp_path / "workbooks" / str(sid)
            session_dir.mkdir(parents=True, exist_ok=True)
            db.add(
                SessionRow(
                    id=sid,
                    workbook_path=str(session_dir / "current.xlsx"),
                )
            )
            plan_json = None
            if idx in (1, 3):
                plan_json = Plan(summary="_", steps=[]).model_dump(mode="json")
            db.add(
                OrchestratorState(
                    session_id=sid,
                    state=state,
                    prompt=f"test-{idx}",
                    plan=plan_json,
                    current_step_index=0,
                )
            )
        await db.commit()

    _patch_agents(
        monkeypatch,
        steps=[],  # empty plan → straight to DONE
        actions_per_step={},
    )

    async with session_factory() as db:
        resumed = await durable.resume_all(db)

    # Two non-terminal sessions (PLANNING and VERIFYING); DONE/FAILED skipped.
    assert len(resumed) == 2


# ---------------------------------------------------------------------------
# Idempotence: replay from checkpoint produces same actions
# ---------------------------------------------------------------------------


async def test_resume_idempotent_no_duplicate_actions(
    sessionmaker_and_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running `resume(sid)` after a session already reached DONE must not
    re-execute steps or append duplicate rows."""
    session_factory = sessionmaker_and_db
    sid = await _seed_session(session_factory, tmp_path)

    _patch_agents(
        monkeypatch,
        steps=[PlanStep(title="only", description="")],
        actions_per_step={
            "only": [WriteValue(sheet="Sheet", target="A1", value=1)],
        },
    )

    async with session_factory() as db:
        await durable.start(db, session_id=sid, prompt="one")
    await durable.wait(sid)

    async with session_factory() as db:
        first_count = len(
            list(
                (
                    await db.execute(
                        select(Action).where(Action.session_id == sid)
                    )
                ).scalars()
            )
        )

    # Resume a terminal session — should no-op on the actions side.
    # (Our implementation starts the task; the phase driver finds state=DONE
    # and skips past the phases.)
    await durable.resume(sid)
    await durable.wait(sid)

    async with session_factory() as db:
        second_count = len(
            list(
                (
                    await db.execute(
                        select(Action).where(Action.session_id == sid)
                    )
                ).scalars()
            )
        )
    assert second_count == first_count


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


async def test_exception_marks_failed_and_emits_terminal(
    sessionmaker_and_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception inside a phase transitions state=FAILED and emits a
    terminal event so subscribers don't hang."""
    session_factory = sessionmaker_and_db
    sid = await _seed_session(session_factory, tmp_path)

    async def broken_plan(prompt: str, workbook_summary: dict) -> Plan:
        raise RuntimeError("planner kaboom")

    monkeypatch.setattr(planner, "plan", broken_plan)
    # Verifier / executor won't be reached, but stub them to keep imports
    # resolved.
    _patch_agents(
        monkeypatch,
        steps=[],
        actions_per_step={},
    )
    # The _patch_agents call overrode planner too — restore broken_plan.
    monkeypatch.setattr(planner, "plan", broken_plan)

    async with session_factory() as db:
        await durable.start(db, session_id=sid, prompt="doomed")
    await durable.wait(sid)

    async with session_factory() as db:
        state_row = await db.get(OrchestratorState, sid)
        assert state_row is not None
        assert state_row.state is OrchestratorStateEnum.FAILED
        assert state_row.error is not None
        assert "kaboom" in state_row.error

    # Terminal event reached the subscriber.
    async def collect() -> list[OrchestratorStateEnum]:
        got = []
        async for event in events.subscribe(sid):
            got.append(event.state)
        return got

    got = await asyncio.wait_for(collect(), timeout=1.0)
    assert got[-1] is OrchestratorStateEnum.FAILED


# ---------------------------------------------------------------------------
# Double-start guard
# ---------------------------------------------------------------------------


async def test_start_raises_on_existing_non_terminal(
    sessionmaker_and_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_factory = sessionmaker_and_db
    sid = await _seed_session(session_factory, tmp_path)

    # Seed an in-flight OrchestratorState without running a task.
    async with session_factory() as db:
        db.add(
            OrchestratorState(
                session_id=sid,
                state=OrchestratorStateEnum.EXECUTING,
                prompt="in-flight",
                plan=None,
                current_step_index=0,
            )
        )
        await db.commit()

    async with session_factory() as db:
        with pytest.raises(RuntimeError, match="non-terminal"):
            await durable.start(db, session_id=sid, prompt="second attempt")


# ---------------------------------------------------------------------------
# checkpoint module directly
# ---------------------------------------------------------------------------


async def test_checkpoint_save_and_load_roundtrip(sessionmaker_and_db) -> None:
    session_factory = sessionmaker_and_db
    sid = uuid.uuid4()

    async with session_factory() as db:
        db.add(SessionRow(id=sid, workbook_path="/tmp/x"))
        await db.commit()

        await checkpoint.create(db, session_id=sid, prompt="hello")
        await db.commit()

        plan = Plan(summary="s", steps=[PlanStep(title="t", description="")])
        await checkpoint.save(
            db, session_id=sid, state=OrchestratorStateEnum.EXECUTING, plan=plan,
            current_step_index=5,
        )
        await db.commit()

        row = await checkpoint.load(db, session_id=sid)
        assert row is not None
        assert row.state is OrchestratorStateEnum.EXECUTING
        assert row.current_step_index == 5
        hydrated = checkpoint.plan_from_row(row)
        assert hydrated is not None
        assert hydrated.steps[0].title == "t"
