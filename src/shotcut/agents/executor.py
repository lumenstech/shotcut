"""Executor agent.

Given one plan step and the current workbook state, the executor emits a
sequence of Action objects by calling spreadsheet tools. We use Claude's
tool-use path rather than structured output because:
  - tool use handles multi-call sequences naturally
  - each tool_use block maps 1:1 to an Action we apply and audit
  - the model can interleave reasoning between cells

The tools here are pure *emit* tools — they return a stub acknowledgement
and the orchestrator is responsible for actually applying the action to
the workbook and writing the audit log. This keeps validation in Python.
"""
from __future__ import annotations

import json
from typing import Any

from shotcut.agents.planner import PlanStep
from shotcut.config import settings
from shotcut.llm.client import cached_system, get_client
from shotcut.spreadsheet.actions import (
    Action,
    AddSheet,
    FormatCell,
    SetColumnWidth,
    WriteFormula,
    WriteValue,
)

SYSTEM_PROMPT = """You are the execution agent in a spreadsheet construction system.

You receive one plan step at a time along with the current workbook state \
and must emit the cell-level actions that implement it.

Rules:
- Emit actions only through the provided tools. Do not respond in prose.
- Every formula must start with '=' and use Excel syntax. Prefer named \
  references via cell addresses over hardcoded numbers.
- Write inputs before outputs; never reference a cell that is not yet \
  populated earlier in the sequence.
- Label every section and column header clearly so a human auditor can \
  read the model.
- When you are done with the step, stop calling tools and return a short \
  text summary of what you wrote.
"""

TOOLS: list[dict] = [
    {
        "name": "write_formula",
        "description": "Write an Excel formula to a cell or range. The formula must start with '='.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sheet": {"type": "string"},
                "target": {"type": "string", "description": "A1 reference or range."},
                "formula": {"type": "string"},
            },
            "required": ["sheet", "target", "formula"],
        },
    },
    {
        "name": "write_value",
        "description": "Write a literal value (string, number, bool) to a cell or range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sheet": {"type": "string"},
                "target": {"type": "string"},
                "value": {},
            },
            "required": ["sheet", "target", "value"],
        },
    },
    {
        "name": "format_cell",
        "description": "Set number format and/or font weight on a cell or range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sheet": {"type": "string"},
                "target": {"type": "string"},
                "number_format": {"type": "string"},
                "bold": {"type": "boolean"},
                "italic": {"type": "boolean"},
            },
            "required": ["sheet", "target"],
        },
    },
    {
        "name": "add_sheet",
        "description": "Create a new worksheet.",
        "input_schema": {
            "type": "object",
            "properties": {"sheet": {"type": "string"}},
            "required": ["sheet"],
        },
    },
    {
        "name": "set_column_width",
        "description": "Set the width of a column. `target` is a cell in the column, e.g. 'A1'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sheet": {"type": "string"},
                "target": {"type": "string"},
                "width": {"type": "number"},
            },
            "required": ["sheet", "target", "width"],
        },
    },
]


def _build_action(name: str, inputs: dict[str, Any]) -> Action:
    if name == "write_formula":
        return WriteFormula(**inputs)
    if name == "write_value":
        return WriteValue(**inputs)
    if name == "format_cell":
        return FormatCell(**inputs)
    if name == "add_sheet":
        return AddSheet(**inputs)
    if name == "set_column_width":
        return SetColumnWidth(**inputs)
    raise ValueError(f"Unknown tool: {name}")


async def execute(step: PlanStep, workbook_summary: dict, max_iterations: int = 6) -> list[Action]:
    """Run a manual tool-use loop; collect Action objects without executing them."""
    client = get_client()
    user_message = (
        f"Plan step:\n"
        f"Title: {step.title}\n"
        f"Description: {step.description}\n"
        f"Target sheet hint: {step.target_sheet or '(unspecified)'}\n\n"
        f"Current workbook state (JSON):\n{json.dumps(workbook_summary, default=str)}"
    )
    messages = [{"role": "user", "content": user_message}]
    actions: list[Action] = []

    for _ in range(max_iterations):
        response = await client.messages.create(
            model=settings.executor_model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=cached_system(SYSTEM_PROMPT),
            tools=TOOLS,
            messages=messages,
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in tool_uses:
            try:
                action = _build_action(block.name, block.input)
                actions.append(action)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"recorded {block.name} for {block.input.get('target', '?')}",
                    }
                )
            except Exception as exc:
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"error: {exc}",
                        "is_error": True,
                    }
                )

        messages.append({"role": "user", "content": tool_results})

        if response.stop_reason == "end_turn":
            break

    return actions
