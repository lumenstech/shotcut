from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
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
)
from shotcut.config import settings
from shotcut.db.models import Action, Session as SessionRow
from shotcut.db.session import get_db

router = APIRouter()


@router.post("/sessions", response_model=SessionCreateResponse)
async def create_session(
    upload: UploadFile | None = None,
    db: AsyncSession = Depends(get_db),
) -> SessionCreateResponse:
    session_id = uuid.uuid4()
    workbook_path = settings.storage_dir / f"{session_id}.xlsx"

    if upload is not None:
        contents = await upload.read()
        workbook_path.write_bytes(contents)
    # If no upload, the orchestrator will start from a blank workbook and
    # save to this path on first run.

    row = SessionRow(id=session_id, workbook_path=str(workbook_path))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return SessionCreateResponse(id=row.id, created_at=row.created_at)


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
    if not input_path.exists():
        input_path_arg = None
    else:
        input_path_arg = input_path

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
