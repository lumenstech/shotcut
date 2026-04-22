"""Cassette schema + loader.

One JSON file per case, at `evals/recording/cassettes/<case_id>.json`:

    {
      "case_id": "sum_of_column",
      "prompt": "...",
      "plan": {
        "summary": "...",
        "steps": [{"title": "...", "description": "..."}]
      },
      "executor_outputs": [
        [
          {"type": "write_formula", "sheet": "...", ...},
          ...
        ],
        ...
      ],
      "verifier": {"findings": [], "confidence": 1.0}
    }

`executor_outputs` is a list of lists — one list of Action blobs per
plan step, in order. Pydantic's discriminated-union validation does
the type-checking on load so cassettes with stale action schemas
fail at load time rather than during replay.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from shotcut.agents.planner import Plan
from shotcut.agents.verifier import VerificationReport
from shotcut.spreadsheet.actions import Action as AgentAction


_CASSETTES_DIR = Path(__file__).resolve().parent / "cassettes"


class CassetteMissing(FileNotFoundError):
    """No cassette is committed for the requested case_id.

    Distinct from a generic FileNotFoundError so callers can
    distinguish "this case isn't recorded yet" from "the cassette
    directory is broken."
    """


class Cassette(BaseModel):
    """Committed agent behavior for one case."""

    case_id: str
    prompt: str
    plan: Plan
    # per-step action lists, in plan-order
    executor_outputs: list[list[AgentAction]]
    verifier: VerificationReport


_cassette_adapter: TypeAdapter[Cassette] = TypeAdapter(Cassette)


def load_cassette(case_id: str, *, root: Path | None = None) -> Cassette:
    """Load `<root>/<case_id>.json` as a Cassette.

    Raises `CassetteMissing` if the file doesn't exist — callers
    (notably the CLI) treat that as a hard error so drift between
    golden/cases.py and the cassette directory is noisy.
    """
    target = (root or _CASSETTES_DIR) / f"{case_id}.json"
    if not target.exists():
        raise CassetteMissing(
            f"no cassette for case_id={case_id!r} at {target}"
        )
    raw: dict[str, Any] = json.loads(target.read_text())
    return _cassette_adapter.validate_python(raw)


def cassettes_dir() -> Path:
    """Committed cassette directory. Tests override via `root=` on
    `load_cassette`; this helper is for tooling that walks the dir."""
    return _CASSETTES_DIR
