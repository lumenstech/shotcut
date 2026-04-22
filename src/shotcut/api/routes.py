from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.agents import orchestrator
from shotcut.api.schemas import (
    ActionOut,
    AuditResponse,
    PromptRequest,
    PromptResponse,
    SessionCreateResponse,
    UploadResponse,
)
from shotcut.config import settings
from shotcut.db.models import Action, Session as SessionRow
from shotcut.db.session import get_db
from shotcut.security.scan import scan_bytes
from shotcut.spreadsheet.parser import parse as parse_workbook
from shotcut.storage import get_storage

router = APIRouter()

ORIGINAL_NAME = "original.xlsx"


@router.post("/sessions", response_model=SessionCreateResponse)
async def create_session(
    db: AsyncSession = Depends(get_db),
) -> SessionCreateResponse:
    """Create a blank session. Upload an .xlsx separately via /upload."""
    session_id = uuid.uuid4()
    # Placeholder path for the orchestrator's first save. Overwritten if
    # the user uploads a workbook before prompting.
    workbook_path = settings.storage_dir / "workbooks" / str(session_id) / "current.xlsx"

    row = SessionRow(id=session_id, workbook_path=str(workbook_path))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return SessionCreateResponse(id=row.id, created_at=row.created_at)


@router.post("/sessions/{session_id}/upload", response_model=UploadResponse)
async def upload_workbook(
    session_id: uuid.UUID,
    upload: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
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

    scan = scan_bytes(contents)
    if not scan.clean:
        raise HTTPException(
            status_code=400,
            detail=f"upload rejected by scanner: {scan.threat}",
        )

    session = await db.get(SessionRow, session_id)
    if session is None:
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


@router.post("/sessions/{session_id}/prompt", response_model=PromptResponse)
async def prompt_session(
    session_id: uuid.UUID,
    body: PromptRequest,
    db: AsyncSession = Depends(get_db),
) -> PromptResponse:
    session = await db.get(SessionRow, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    input_path = Path(session.workbook_path)
    input_path_arg: Path | None = input_path if input_path.exists() else None

    result = await orchestrator.run(
        db,
        session_id=session_id,
        prompt=body.prompt,
        input_path=input_path_arg,
    )

    return PromptResponse(
        session_id=session_id,
        plan=result.plan,
        actions_applied=result.actions_applied,
        syntactic_issues=[dataclasses.asdict(i) for i in result.syntactic_issues],
        verification=result.verification,
        download_path=f"/sessions/{session_id}/workbook",
    )


@router.get("/sessions/{session_id}/workbook")
async def download_workbook(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    session = await db.get(SessionRow, session_id)
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
) -> AuditResponse:
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
                created_at=r.created_at,
            )
            for r in rows
        ],
    )
