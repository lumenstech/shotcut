"""Replay producer: turn a committed cassette into a Workbook.

`recorded_trace_producer(case_id)` returns a `Producer` closure that
the eval runner calls with the case's prompt. The closure ignores the
prompt (the cassette was recorded against it; drift would invalidate
the cassette, which the scorer then catches) and walks the recorded
action list against a fresh blank Workbook.

This is pragmatic middle-ground scope — the Anthropic SDK layer is
not exercised here (tests/observability/test_llm_wiring.py covers
that); the agent-layer orchestration state machine isn't either
(that requires a throwaway DB, which is more surface than the
regression we're trying to prevent). What *is* exercised:

- Action schema stability (cassettes fail to load on schema drift)
- `Workbook.apply()` for every action type
- The scorer's ability to validate the resulting workbook against
  the committed expected output

Missing cassettes raise `CassetteMissing` at closure-build time, so
adding a golden case without recording a cassette fails immediately,
not silently-as-an-identity-producer.
"""
from __future__ import annotations

from shotcut.spreadsheet.workbook import Workbook

from evals.recording.cassette import load_cassette
from evals.runner import Producer


def recorded_trace_producer(case_id: str) -> Producer:
    """Build a Producer that replays the cassette for `case_id`.

    The cassette is loaded eagerly at closure-build time so CassetteMissing
    surfaces during CLI startup rather than mid-run. Once built, the
    closure is cheap to invoke — one Workbook.blank() + action loop
    per call.
    """
    cassette = load_cassette(case_id)

    async def produce(_prompt: str) -> Workbook:
        # The prompt parameter is unused — the cassette IS the record
        # of what the agents produced for this prompt, and the runner
        # already matched case→prompt via the EvalCase.prompt field.
        # Keeping the parameter honors the Producer type contract.
        workbook = Workbook.blank()
        for step_actions in cassette.executor_outputs:
            for action in step_actions:
                workbook.apply(action)
        return workbook

    return produce
