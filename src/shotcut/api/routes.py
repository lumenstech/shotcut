from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.agents import orchestrator as sync_orchestrator
from shotcut.api.schemas import (
    ActionOut,
    ApprovalResponse,
    AuditResponse,
    BranchRequest,
    BranchResponse,
    PromptAccepted,
    PromptRequest,
    SessionCreateResponse,
    UndoResponse,
    UploadResponse,
)
from shotcut.audit import branch as branch_mod
from shotcut.audit import export as export_mod
from shotcut.audit import replay as replay_mod
from shotcut.audit import undo as undo_mod
from shotcut.auth.middleware import get_current_user
from shotcut.auth.tenancy import UserContext, scope_to_tenant
from shotcut.config import settings
from shotcut.db.models import Action, ActionStatus, OrchestratorStateEnum
from shotcut.db.models import Session as SessionRow
from shotcut.db.session import get_db
from shotcut.orchestrator import checkpoint as checkpoint_mod
from shotcut.orchestrator import durable as durable_mod
from shotcut.orchestrator import events as events_mod
from shotcut.orchestrator.events import ProgressEvent
from shotcut.security.scan import scan_bytes
from shotcut.spreadsheet.parser import parse as parse_workbook
from shotcut.storage import get_storage

# Every route in this router requires a valid authenticated user.
# Individual handlers that need the UserContext take it as a
# `user: UserContext = Depends(get_current_user)` parameter — FastAPI
# shares the cached resolution with this router-level dependency so
# the verify+claims-extract work runs exactly once per request.
router = APIRouter(dependencies=[Depends(get_current_user)])

ORIGINAL_NAME = "original.xlsx"


async def _load_scoped_session(
    db: AsyncSession, session_id: uuid.UUID, user: UserContext
) -> SessionRow | None:
    """Tenant-scoped session lookup.

    Returns the row only if its `tenant_id` matches the authenticated
    user's. This is the application-layer half of Stage 8's
    defense-in-depth; Postgres RLS (migration 0006) is the other half
    and enforces the same constraint on non-auth'd connections.

    Carve-out: the anonymous-sentinel tenant (AUTH_DISABLED dev/test
    mode) additionally matches rows with `tenant_id IS NULL` so pre-
    Stage-8 fixtures continue to work without rewriting them to
    populate the zero UUID. Real-tenant users never hit this branch.
    """
    anonymous_tenant = UserContext.anonymous().tenant_id
    if user.tenant_id == anonymous_tenant:
        stmt = (
            select(SessionRow)
            .where(SessionRow.id == session_id)
            .where(
                (SessionRow.tenant_id == anonymous_tenant)
                | SessionRow.tenant_id.is_(None)
            )
        )
    else:
        stmt = (
            select(SessionRow)
            .where(SessionRow.id == session_id)
            .where(SessionRow.tenant_id == user.tenant_id)
        )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _enter_tenant_scope(db: AsyncSession, user: UserContext) -> None:
    """Set the Postgres session var so RLS policies fire. No-op on SQLite."""
    await scope_to_tenant(db, user.tenant_id)


@router.post("/sessions", response_model=SessionCreateResponse)
async def create_session(
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(get_current_user),
) -> SessionCreateResponse:
    """Create a blank session. Upload an .xlsx separately via /upload."""
    await _enter_tenant_scope(db, user)
    session_id = uuid.uuid4()
    # Placeholder path for the orchestrator's first save. Overwritten if
    # the user uploads a workbook before prompting.
    workbook_path = settings.storage_dir / "workbooks" / str(session_id) / "current.xlsx"

    row = SessionRow(
        id=session_id,
        workbook_path=str(workbook_path),
        tenant_id=user.tenant_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return SessionCreateResponse(id=row.id, created_at=row.created_at)


@router.post("/sessions/{session_id}/upload", response_model=UploadResponse)
async def upload_workbook(
    session_id: uuid.UUID,
    upload: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(get_current_user),
) -> UploadResponse:
    """Ingest an uploaded .xlsx. Stores the original, parses it for metadata,
    and points the session at it as the starting workbook state.
    """
    # Size and scan checks run before the DB lookup — they're cheap, and
    # failing them on a nonexistent session still gives the user actionable
    # information (shrink your file) rather than a 404 they can't fix.
    max_bytes = settings.max_upload_mb * 1024 * 1024
    contents = await upload.read()
    if len(contents) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds {settings.max_upload_mb} MB limit",
        )

    scan = await scan_bytes(contents)
    if not scan.clean:
        raise HTTPException(
            status_code=400,
            detail=f"upload rejected by scanner: {scan.threat}",
        )

    await _enter_tenant_scope(db, user)
    session = await _load_scoped_session(db, session_id, user)
    if session is None:
        # 404 on cross-tenant access too — don't leak existence of other
        # tenants' sessions via a 403 distinction.
        raise HTTPException(status_code=404, detail="session not found")

    storage = get_storage()
    storage.put(session_id, ORIGINAL_NAME, contents)

    local = storage.local_path(session_id, ORIGINAL_NAME)
    if local is None:
        # Remote-only backend (S3 etc.). Spool to a temp file for openpyxl.
        raise HTTPException(
            status_code=500,
            detail="remote-only storage not yet supported by the parser",
        )

    parsed = parse_workbook(local)

    session.original_workbook_path = str(local)
    session.workbook_path = str(local)
    await db.commit()

    return UploadResponse(
        session_id=session_id,
        size_bytes=len(contents),
        metadata=parsed.metadata,
    )


@router.post(
    "/sessions/{session_id}/prompt",
    response_model=PromptAccepted,
    status_code=202,
)
async def prompt_session(
    session_id: uuid.UUID,
    body: PromptRequest,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(get_current_user),
) -> PromptAccepted:
    """Start a durable orchestrator run for `session_id`.

    Returns 202 immediately — the run proceeds in a background task.
    Clients subscribe to `GET /sessions/{id}/events` for progress and
    fetch `/workbook`, `/audit` when state=done.
    """
    await _enter_tenant_scope(db, user)
    session = await _load_scoped_session(db, session_id, user)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    await durable_mod.start(db, session_id=session_id, prompt=body.prompt, user=user)

    return PromptAccepted(
        session_id=session_id,
        events_path=f"/sessions/{session_id}/events",
        audit_path=f"/sessions/{session_id}/audit",
        workbook_path=f"/sessions/{session_id}/workbook",
    )


@router.get("/sessions/{session_id}/events")
async def stream_session_events(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(get_current_user),
) -> StreamingResponse:
    """Server-Sent Events stream of orchestrator progress.

    Subscribes to the in-process event bus. If the session is already
    in a terminal state when this endpoint is hit, emits one synthetic
    event and closes so late subscribers still see completion.
    """
    await _enter_tenant_scope(db, user)
    session = await _load_scoped_session(db, session_id, user)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    state_row = await checkpoint_mod.load(db, session_id=session_id)
    terminal_state: OrchestratorStateEnum | None = None
    terminal_error: str | None = None
    if state_row is not None and state_row.state in (
        OrchestratorStateEnum.DONE,
        OrchestratorStateEnum.FAILED,
    ):
        terminal_state = state_row.state
        terminal_error = state_row.error

    async def generator() -> AsyncIterator[str]:
        if terminal_state is not None:
            synthetic = ProgressEvent(
                session_id=session_id,
                state=terminal_state,
                message=terminal_error or "complete",
                percent=1.0,
            )
            yield f"data: {synthetic.model_dump_json()}\n\n"
            return
        async for event in events_mod.subscribe(session_id):
            yield f"data: {event.model_dump_json()}\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")


@router.post(
    "/sessions/{session_id}/actions/{action_id}/approve",
    response_model=ApprovalResponse,
)
async def approve_action(
    session_id: uuid.UUID,
    action_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(get_current_user),
) -> ApprovalResponse:
    """Apply a previously-staged pending_approval action with force_override=True."""
    await _enter_tenant_scope(db, user)
    # Tenant-scope via the owning session; cross-tenant action lookups 404.
    session = await _load_scoped_session(db, session_id, user)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    row = await db.get(Action, action_id)
    if row is None or row.session_id != session_id:
        raise HTTPException(status_code=404, detail="action not found for session")
    if row.status is not ActionStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"action is {row.status.value}, not pending_approval",
        )

    result = await sync_orchestrator.apply_pending_action(db, action_row=row)
    return ApprovalResponse(
        action_id=row.id,
        status=row.status,
        previous_value=result["previous_value"],  # type: ignore[arg-type]
    )


@router.post(
    "/sessions/{session_id}/actions/{action_id}/reject",
    response_model=ApprovalResponse,
)
async def reject_action(
    session_id: uuid.UUID,
    action_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(get_current_user),
) -> ApprovalResponse:
    """Mark a pending action as rejected. Terminal — does not touch the workbook."""
    await _enter_tenant_scope(db, user)
    session = await _load_scoped_session(db, session_id, user)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    row = await db.get(Action, action_id)
    if row is None or row.session_id != session_id:
        raise HTTPException(status_code=404, detail="action not found for session")
    if row.status is not ActionStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"action is {row.status.value}, not pending_approval",
        )
    row.status = ActionStatus.REJECTED
    await db.commit()
    return ApprovalResponse(action_id=row.id, status=row.status)


# ---------------------------------------------------------------------------
# Stage 5: replay, branch, undo, audit export
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/replay/{sequence}")
async def replay_to_sequence(
    session_id: uuid.UUID,
    sequence: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(get_current_user),
) -> FileResponse:
    """Reconstruct the workbook state at the given sequence number and
    return it as an .xlsx download. Does not mutate the session's
    persisted workbook."""
    await _enter_tenant_scope(db, user)
    session = await _load_scoped_session(db, session_id, user)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    workbook = await replay_mod.replay_to(
        db, session_id=session_id, up_to_sequence=sequence
    )

    storage = get_storage()
    out_path = storage.local_path(session_id, f"replay_{sequence}.xlsx")
    assert out_path is not None
    workbook.save(out_path)
    return FileResponse(
        out_path,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        filename=f"{session_id}-replay-{sequence}.xlsx",
    )


@router.post(
    "/sessions/{session_id}/branch",
    response_model=BranchResponse,
)
async def branch_session(
    session_id: uuid.UUID,
    body: BranchRequest,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(get_current_user),
) -> BranchResponse:
    """Fork the session at `at_sequence`. Returns the new child
    session id — subsequent actions go to the child independently."""
    await _enter_tenant_scope(db, user)
    parent = await _load_scoped_session(db, session_id, user)
    if parent is None:
        raise HTTPException(status_code=404, detail="session not found")

    child = await branch_mod.fork_at(
        db,
        parent_session_id=session_id,
        at_sequence=body.at_sequence,
        title=body.title,
        tenant_id=user.tenant_id,
    )
    return BranchResponse(
        child_session_id=child.id,
        parent_session_id=session_id,
        branched_at_sequence=body.at_sequence,
    )


@router.post(
    "/sessions/{session_id}/actions/{action_id}/undo",
    response_model=UndoResponse,
)
async def undo_action(
    session_id: uuid.UUID,
    action_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(get_current_user),
) -> UndoResponse:
    """Create and apply the inverse of `action_id`. Original row's status
    flips to UNDONE; inverse is inserted as a new APPLIED row with
    `parent_action_id` pointing at the original."""
    await _enter_tenant_scope(db, user)
    session = await _load_scoped_session(db, session_id, user)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    row = await db.get(Action, action_id)
    if row is None or row.session_id != session_id:
        raise HTTPException(status_code=404, detail="action not found for session")

    try:
        inverse_row = await undo_mod.undo_action(db, action_row=row)
    except undo_mod.UndoUnsupported as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return UndoResponse(
        original_action_id=row.id,
        inverse_action_id=inverse_row.id,
        inverse_sequence=inverse_row.sequence,
    )


@router.get(
    "/sessions/{session_id}/audit/export",
    response_class=PlainTextResponse,
)
async def export_audit(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(get_current_user),
) -> PlainTextResponse:
    """Return the session's audit history rendered as markdown."""
    await _enter_tenant_scope(db, user)
    session = await _load_scoped_session(db, session_id, user)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    markdown = await export_mod.render_markdown(db, session_id=session_id)
    return PlainTextResponse(content=markdown, media_type="text/markdown")


# ---------------------------------------------------------------------------
# Downloads / audit (Stage 0 + Stage 3)
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/workbook")
async def download_workbook(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(get_current_user),
) -> FileResponse:
    await _enter_tenant_scope(db, user)
    session = await _load_scoped_session(db, session_id, user)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    path = Path(session.workbook_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="workbook not yet generated")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{session_id}.xlsx",
    )


@router.get("/sessions/{session_id}/audit", response_model=AuditResponse)
async def get_audit(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(get_current_user),
) -> AuditResponse:
    await _enter_tenant_scope(db, user)
    session = await _load_scoped_session(db, session_id, user)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    stmt = (
        select(Action)
        .where(Action.session_id == session_id)
        .order_by(Action.sequence)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return AuditResponse(
        session_id=session_id,
        actions=[
            ActionOut(
                sequence=r.sequence,
                agent=r.agent,
                action_type=r.action_type,
                sheet=r.sheet,
                target_range=r.target_range,
                new_value=r.new_value,
                reasoning=r.reasoning,
                status=r.status,
                approval_required_reason=r.approval_required_reason,
                force_override=r.force_override,
                created_at=r.created_at,
            )
            for r in rows
        ],
    )
