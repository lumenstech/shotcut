"""Recorded-trace producer: replay committed agent outputs against a
blank workbook to exercise the apply-path + scorer end-to-end without
hitting Anthropic.

This is the Stage 10 follow-up noted in docs/decisions/0007-eval-suite.md
— the identity producer exercised the scoring framework but didn't
touch agent machinery. The replay producer reads a committed cassette
(Plan + per-step action list) and applies it to a fresh Workbook so
CI catches:

- Action schema drift (Pydantic validation fails on cassette load)
- Workbook.apply regressions (cassettes that used to land produce
  scorer mismatches)
- Scorer regressions (identical actions + workbook → different diff
  labeling)

Cassettes are agent-layer artifacts, not HTTP-layer recordings. The
pragmatic scope here is to test "given these actions, does the system
produce the expected workbook" — the Anthropic SDK surface is tested
separately in tests/observability/test_llm_wiring.py.
"""
from evals.recording.cassette import Cassette, CassetteMissing, load_cassette
from evals.recording.producer import recorded_trace_producer

__all__ = [
    "Cassette",
    "CassetteMissing",
    "load_cassette",
    "recorded_trace_producer",
]
