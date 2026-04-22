"""Durable, resumable, observable orchestrator.

Wraps the planner → executor → verifier pipeline with per-phase
checkpointing so a worker restart picks up where it left off, and
per-phase event emission so the SSE endpoint can stream progress.

See docs/decisions/0003-durable-execution.md for the state machine,
checkpoint granularity, and upgrade path to Redis pub/sub.

Key behaviors:
- `start()` creates the OrchestratorState row and spawns a background
  asyncio task. Returns immediately.
- `resume()` re-attaches to a non-terminal session's task. Used by
  the startup scan and explicit retry callers.
- `wait()` awaits the in-process task for a session. Test-only helper;
  production subscribes to events.
- The background task opens its own DB session (independent of the
  request's) so lifecycle is independent.
- On exception, the task marks state=FAILED with the error message
  and emits a terminal event.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.agents import executor, planner, verifier
from shotcut.db import audit, session as db_session
from shotcut.db.models import ActionStatus, OrchestratorStateEnum
from shotcut.db.models import Session as SessionRow
from shotcut.orchestrator import checkpoint, events
from shotcut.orchestrator.events import ProgressEvent
from shotcut.spreadsheet.occupancy import OccupancyMap
from shotcut.spreadsheet.workbook import Workbook
from shotcut.storage import get_storage

log = logging.getLogger(__name__)


# In-process task registry — the background asyncio.Task for each
# active session. Tests call `wait()` to await completion; production
# discards the reference (the event loop holds its own strong ref via
# create_task + we track here to prevent "never awaited" warnings).
_running_tasks: dict[uuid.UUID, asyncio.Task[None]] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def start(
    db: AsyncSession, *, session_id: uuid.UUID, prompt: str
) -> None:
    """Begin a durable orchestrator run for `session_id`.

    Creates the OrchestratorState row (state=PLANNING) using the
    caller's `db`, commits, then spawns a background task that uses
    its own DB session.
    """
    existing = await checkpoint.load(db, session_id=session_id)
    if existing is not None and existing.state not in (
        OrchestratorStateEnum.DONE,
        OrchestratorStateEnum.FAILED,
    ):
        raise RuntimeError(
            f"session {session_id} already has a non-terminal orchestrator state "
            f"({existing.state.value}); use resume() instead"
        )

    if existing is None:
        await checkpoint.create(db, session_id=session_id, prompt=prompt)
    else:
        # Re-running a terminal session: reset the row for a fresh run.
        existing.state = OrchestratorStateEnum.PLANNING
        existing.prompt = prompt
        existing.plan = None
        existing.current_step_index = 0
        existing.error = None
    await db.commit()

    _spawn(session_id)


async def resume(session_id: uuid.UUID) -> None:
    """Re-attach to a non-terminal session. No-op if a task is already running."""
    if session_id in _running_tasks and not _running_tasks[session_id].done():
        return
    _spawn(session_id)


async def resume_all(db: AsyncSession) -> list[uuid.UUID]:
    """Startup scan: find every non-terminal OrchestratorState row and
    resume each. Returns the list of session ids it attached to."""
    rows = await checkpoint.find_non_terminal(db)
    resumed: list[uuid.UUID] = []
    for row in rows:
        await resume(row.session_id)
        resumed.append(row.session_id)
    return resumed


async def wait(session_id: uuid.UUID) -> None:
    """Await the background task for `session_id`. Test-only helper."""
    task = _running_tasks.get(session_id)
    if task is not None:
        await task


def reset_for_tests() -> None:
    """Drop all task references. Does NOT cancel running tasks — they
    complete normally. Per-test isolation helper."""
    _running_tasks.clear()


# ---------------------------------------------------------------------------
# Background task driver
# ---------------------------------------------------------------------------


def _spawn(session_id: uuid.UUID) -> None:
    task = asyncio.create_task(_run_with_fresh_session(session_id))
    _running_tasks[session_id] = task


async def _run_with_fresh_session(session_id: uuid.UUID) -> None:
    """Opens a new DB session for the background task's lifetime.

    Catches every exception so a crash in one phase persists as
    state=FAILED rather than silently bubbling to the event loop.

    `db_session.SessionLocal` is resolved at call time so tests can
    swap it in per-test fixtures.
    """
    try:
        async with db_session.SessionLocal() as db:
            try:
                await _run(db, session_id)
            except Exception as exc:  # noqa: BLE001 — last-chance handler
                log.exception("orchestrator: session=%s failed", session_id)
                await _mark_failed(db, session_id, exc)
    finally:
        _running_tasks.pop(session_id, None)


async def _mark_failed(
    db: AsyncSession, session_id: uuid.UUID, exc: BaseException
) -> None:
    try:
        await checkpoint.save(
            db,
            session_id=session_id,
            state=OrchestratorStateEnum.FAILED,
            error=f"{type(exc).__name__}: {exc}",
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("orchestrator: could not persist FAILED state for %s", session_id)
    await events.emit(
        session_id,
        ProgressEvent(
            session_id=session_id,
            state=OrchestratorStateEnum.FAILED,
            message=f"run failed: {exc}",
            percent=1.0,
        ),
    )


# ---------------------------------------------------------------------------
# Phase driver
# ---------------------------------------------------------------------------


async def _run(db: AsyncSession, session_id: uuid.UUID) -> None:
    state = await checkpoint.load(db, session_id=session_id)
    if state is None:
        raise RuntimeError(f"no orchestrator state for session {session_id}")

    session_row = await db.get(SessionRow, session_id)
    if session_row is None:
        raise RuntimeError(f"session {session_id} does not exist")

    workbook = _load_workbook(session_row)
    occupancy = _load_occupancy(session_row)

    # Phase 1: PLANNING (idempotent — we cache the plan on success).
    plan = checkpoint.plan_from_row(state)
    if plan is None or state.state is OrchestratorStateEnum.PLANNING:
        await events.emit(
            session_id,
            ProgressEvent(
                session_id=session_id,
                state=OrchestratorStateEnum.PLANNING,
                message="planning",
                percent=0.0,
            ),
        )
        plan = await planner.plan(state.prompt, workbook.summary())
        await checkpoint.save(
            db,
            session_id=session_id,
            state=OrchestratorStateEnum.EXECUTING,
            plan=plan,
            current_step_index=0,
        )
        await db.commit()

    # Refresh post-save.
    state = await checkpoint.load(db, session_id=session_id)
    assert state is not None
    total_steps = len(plan.steps)

    # Phase 2: EXECUTING (step by step; checkpoint after each).
    if state.state in (OrchestratorStateEnum.EXECUTING,):
        for idx in range(state.current_step_index, total_steps):
            step = plan.steps[idx]
            await events.emit(
                session_id,
                ProgressEvent(
                    session_id=session_id,
                    state=OrchestratorStateEnum.EXECUTING,
                    message=f"step {idx + 1}/{total_steps}: {step.title}",
                    percent=idx / max(total_steps, 1),
                    current_action=step.title,
                ),
            )
            actions = await executor.execute(step, workbook.summary())
            for action in actions:
                check = occupancy.check(action)
                if check.blocked:
                    await audit.record(
                        db,
                        session_id=session_id,
                        agent="executor",
                        action=action,
                        previous_value=None,
                        reasoning=step.title,
                        status=ActionStatus.PENDING_APPROVAL,
                        approval_required_reason=check.reason,
                        force_override=False,
                    )
                else:
                    previous = workbook.apply(action)
                    await audit.record(
                        db,
                        session_id=session_id,
                        agent="executor",
                        action=action,
                        previous_value=previous,
                        reasoning=step.title,
                        status=ActionStatus.APPLIED,
                    )

            # Checkpoint the step boundary. The audit rows above and
            # this index bump commit atomically — either the step is
            # fully persisted or we re-run it on resume.
            await checkpoint.save(
                db, session_id=session_id, current_step_index=idx + 1
            )
            await db.commit()

        await checkpoint.save(
            db, session_id=session_id, state=OrchestratorStateEnum.VERIFYING
        )
        await db.commit()

    # Phase 3: VERIFYING.
    state = await checkpoint.load(db, session_id=session_id)
    assert state is not None
    if state.state is OrchestratorStateEnum.VERIFYING:
        await events.emit(
            session_id,
            ProgressEvent(
                session_id=session_id,
                state=OrchestratorStateEnum.VERIFYING,
                message="verifying",
                percent=0.95,
            ),
        )
        # Pending actions from this run are surfaced as part of verify's
        # clone-with-pending semantics; see agents.verifier.verify.
        report = await verifier.verify(workbook, prompt=state.prompt)
        log.info(
            "durable: session=%s findings=%d critical=%d",
            session_id,
            len(report.findings),
            len(report.critical),
        )

        # Persist the final workbook state.
        storage = get_storage()
        current_path = storage.local_path(session_id, "current.xlsx")
        assert current_path is not None
        workbook.save(current_path)
        session_row.workbook_path = str(current_path)

        await checkpoint.save(
            db, session_id=session_id, state=OrchestratorStateEnum.DONE
        )
        await db.commit()

    # Terminal event.
    await events.emit(
        session_id,
        ProgressEvent(
            session_id=session_id,
            state=OrchestratorStateEnum.DONE,
            message="complete",
            percent=1.0,
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_workbook(session_row: SessionRow) -> Workbook:
    path = Path(session_row.workbook_path)
    return Workbook.from_xlsx(path) if path.exists() else Workbook.blank()


def _load_occupancy(session_row: SessionRow) -> OccupancyMap:
    if not session_row.original_workbook_path:
        return OccupancyMap.empty()
    path = Path(session_row.original_workbook_path)
    if not path.exists():
        return OccupancyMap.empty()
    return OccupancyMap.from_original(Workbook.from_xlsx(path))
