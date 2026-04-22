"""Planner agent.

Decomposes the user prompt + workbook context into an ordered list of
well-scoped steps that the executor can attack one at a time. Returns
structured output via `client.messages.parse()`.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from shotcut.config import settings
from shotcut.llm.client import cached_system, get_client

SYSTEM_PROMPT = """You are the planning agent in a spreadsheet construction system.

Your job: given a user request and the current workbook state, produce an \
ordered list of concrete, bounded steps that a downstream execution agent \
will carry out one at a time.

Guidelines:
- Each step should be small enough to describe the cells and formulas it \
  will write, but large enough to be meaningful (not one cell per step).
- Prefer formula-driven outputs over static values. Reference inputs by \
  cell address so the model stays live.
- Order steps so dependencies resolve in sequence (inputs before outputs, \
  raw data before derived metrics, subtotals before totals).
- If the request implies multiple worksheets (e.g. Inputs / Model / Summary), \
  plan an add_sheet step before writing to a sheet that does not exist.
- Do not invent data the user did not supply. If a required input is \
  missing, include a step that writes placeholder cells and clearly label \
  them so the user can fill in.
"""


class PlanStep(BaseModel):
    title: str
    description: str
    target_sheet: str | None = None
    depends_on: list[int] = []


class Plan(BaseModel):
    summary: str
    steps: list[PlanStep]


async def plan(prompt: str, workbook_summary: dict[str, Any]) -> Plan:
    client = get_client()
    user_message = (
        f"User request:\n{prompt}\n\n"
        f"Current workbook state (JSON):\n{json.dumps(workbook_summary, default=str)}"
    )
    response = await client.messages.parse(
        model=settings.planner_model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=cached_system(SYSTEM_PROMPT),
        messages=[{"role": "user", "content": user_message}],
        output_format=Plan,
    )
    result = response.parsed_output
    if result is None:
        raise RuntimeError("planner: model returned no parseable output")
    return result
