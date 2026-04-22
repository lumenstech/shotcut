from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from shotcut.agents.planner import Plan
from shotcut.agents.verifier import VerificationReport
from shotcut.spreadsheet.parser import ParseMetadata


class SessionCreateResponse(BaseModel):
    id: uuid.UUID
    created_at: datetime


class PromptRequest(BaseModel):
    prompt: str


class PromptResponse(BaseModel):
    session_id: uuid.UUID
    plan: Plan
    actions_applied: int
    syntactic_issues: list[dict[str, Any]]
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
    created_at: datetime


class AuditResponse(BaseModel):
    session_id: uuid.UUID
    actions: list[ActionOut]


class UploadResponse(BaseModel):
    session_id: uuid.UUID
    size_bytes: int
    metadata: ParseMetadata
