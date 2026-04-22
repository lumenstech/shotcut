from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from shotcut.agents.planner import Plan
from shotcut.agents.verifier import VerificationReport
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


class PromptResponse(BaseModel):
    session_id: uuid.UUID
    plan: Plan
    actions_applied: int
    pending_approvals: list[PendingApprovalOut]
    # VerificationReport carries findings from all 5 levels (syntax /
    # reference / cycle / numerical / semantic). The old separate
    # `syntactic_issues` field folded in at Stage 4.
    verification: VerificationReport
    download_path: str


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
