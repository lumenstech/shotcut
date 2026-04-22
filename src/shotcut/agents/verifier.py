"""Verifier agent.

Two-pass validation:
  1. Deterministic pass (engine.py) over every formula in the workbook.
  2. LLM pass that looks for semantic issues (wrong references, off-by-one
     ranges, numbers that should be formulas, missing totals) and returns
     a structured report.
"""
from __future__ import annotations

import json

from pydantic import BaseModel

from shotcut.config import settings
from shotcut.llm.client import cached_system, get_client
from shotcut.spreadsheet.engine import ValidationIssue, validate_formula
from shotcut.spreadsheet.workbook import Workbook

SYSTEM_PROMPT = """You are the verification agent in a spreadsheet construction system.

You receive the final workbook state and the user's original request. Find \
issues that would make the model incorrect or misleading:
- formulas referencing cells that are blank or obviously wrong
- totals that don't match their components
- hardcoded values where a formula would be correct
- missing headers, labels, or units
- inconsistent number formats across comparable cells

Report only concrete issues with a cell reference. Do not flag stylistic \
concerns. If the workbook looks correct, return an empty issues list.
"""


class SemanticIssue(BaseModel):
    sheet: str
    cell: str
    severity: str  # "error" | "warning"
    message: str


class VerificationReport(BaseModel):
    issues: list[SemanticIssue]
    confidence: float  # 0.0 - 1.0


def syntactic_issues(workbook: Workbook) -> list[ValidationIssue]:
    """Walk every cell, run engine.validate_formula on anything that looks
    like a formula (starts with '=')."""
    issues: list[ValidationIssue] = []
    summary = workbook.summary(max_cells_per_sheet=10_000)
    for sheet in summary["sheets"]:
        for cell in sheet["cells"]:
            value = cell["value"]
            if isinstance(value, str) and value.startswith("="):
                issues.extend(validate_formula(value))
    return issues


async def verify_semantics(prompt: str, workbook: Workbook) -> VerificationReport:
    client = get_client()
    summary = workbook.summary(max_cells_per_sheet=500)
    user_message = (
        f"Original request:\n{prompt}\n\n"
        f"Workbook state (JSON):\n{json.dumps(summary, default=str)}"
    )
    response = await client.messages.parse(
        model=settings.verifier_model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=cached_system(SYSTEM_PROMPT),
        messages=[{"role": "user", "content": user_message}],
        output_format=VerificationReport,
    )
    return response.parsed_output
