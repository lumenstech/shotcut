from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from shotcut.db.models import ActionStatus
from shotcut.spreadsheet.parser import ParseMetadata


class SessionCreateResponse(BaseModel):
    id: uuid.UUID
    created_at: datetime


class PromptRequest(BaseModel):
    prompt: str


class PendingApprovalOut(BaseModel):
    action_id: uuid.UUID
    sheet: str | None
    target: str | None
    action_type: str
    reason: str


class PromptAccepted(BaseModel):
    """Stage 6: POST /prompt returns 202 + this envelope while the
    durable orchestrator runs in the background. Clients subscribe to
    `GET /sessions/{id}/events` for progress, then fetch the audit log
    and workbook when state=done."""

    session_id: uuid.UUID
    events_path: str
    audit_path: str
    workbook_path: str


class ActionOut(BaseModel):
    sequence: int
    agent: str
    action_type: str
    sheet: str | None
    target_range: str | None
    new_value: dict[str, Any] | None
    reasoning: str | None
    status: ActionStatus
    approval_required_reason: str | None
    force_override: bool
    created_at: datetime


class ApprovalResponse(BaseModel):
    action_id: uuid.UUID
    status: ActionStatus
    previous_value: dict[str, Any] | None = None


class AuditResponse(BaseModel):
    session_id: uuid.UUID
    actions: list[ActionOut]


class UploadResponse(BaseModel):
    session_id: uuid.UUID
    size_bytes: int
    metadata: ParseMetadata


class BranchRequest(BaseModel):
    at_sequence: int
    title: str | None = None


class BranchResponse(BaseModel):
    child_session_id: uuid.UUID
    parent_session_id: uuid.UUID
    branched_at_sequence: int


class UndoResponse(BaseModel):
    original_action_id: uuid.UUID
    inverse_action_id: uuid.UUID
    inverse_sequence: int
