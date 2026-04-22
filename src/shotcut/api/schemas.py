from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from shotcut.agents.planner import Plan
from shotcut.agents.verifier import VerificationReport


class SessionCreateResponse(BaseModel):
    id: uuid.UUID
    created_at: datetime


class PromptRequest(BaseModel):
    prompt: str


class PromptResponse(BaseModel):
    session_id: uuid.UUID
    plan: Plan
    actions_applied: int
    syntactic_issues: list[dict]
    verification: VerificationReport
    download_path: str


class ActionOut(BaseModel):
    sequence: int
    agent: str
    action_type: str
    sheet: str | None
    target_range: str | None
    new_value: dict | None
    reasoning: str | None
    created_at: datetime


class AuditResponse(BaseModel):
    session_id: uuid.UUID
    actions: list[ActionOut]
