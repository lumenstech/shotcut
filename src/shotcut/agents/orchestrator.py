"""Async orchestrator that wires planner → executor → verifier together.

This is the code equivalent of the diagram: one in-process state machine,
durably logged via the audit layer. For the MVP it runs the graph
synchronously (from the caller's perspective via `await`); productionizing
would move this behind Temporal or Celery with checkpointing per step.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.agents import executor, planner, verifier
from shotcut.agents.planner import Plan
from shotcut.agents.verifier import VerificationReport
from shotcut.config import settings
from shotcut.db import audit
from shotcut.db.models import Session as SessionRow
from shotcut.spreadsheet.validator import ValidationIssue
from shotcut.spreadsheet.workbook import Workbook

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    plan: Plan
    actions_applied: int
    syntactic_issues: list[ValidationIssue]
    verification: VerificationReport
    output_path: Path


async def run(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    prompt: str,
    input_path: Path | None,
) -> RunResult:
    workbook = Workbook.load(input_path) if input_path else Workbook.blank()

    log.info("planner: starting for session=%s", session_id)
    plan = await planner.plan(prompt, workbook.summary())
    log.info("planner: %d steps", len(plan.steps))

    applied = 0
    for step in plan.steps:
        log.info("executor: step '%s'", step.title)
        actions = await executor.execute(step, workbook.summary())
        for action in actions:
            previous = workbook.apply(action)
            await audit.record(
                db,
                session_id=session_id,
                agent="executor",
                action=action,
                previous_value=previous,
                reasoning=step.title,
            )
            applied += 1
        await db.commit()

    log.info("verifier: syntactic pass")
    syntactic = verifier.syntactic_issues(workbook)
    log.info("verifier: %d syntactic issues", len(syntactic))

    log.info("verifier: semantic pass")
    report = await verifier.verify_semantics(prompt, workbook)
    log.info("verifier: %d semantic issues (confidence=%.2f)", len(report.issues), report.confidence)

    output_path = settings.storage_dir / f"{session_id}.xlsx"
    workbook.save(output_path)

    session_row = await db.get(SessionRow, session_id)
    if session_row:
        session_row.workbook_path = str(output_path)
        await db.commit()

    return RunResult(
        plan=plan,
        actions_applied=applied,
        syntactic_issues=syntactic,
        verification=report,
        output_path=output_path,
    )
