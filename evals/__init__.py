"""Stage 10 eval suite: runner, scoring, regression gate.

Framework only; CI runs the committed golden set. Full SpreadsheetBench
runs on a weekly cron against an external dataset — the runner here
accepts any file path that matches the expected schema.

See docs/decisions/0007-eval-suite.md.
"""
from evals import regression, runner, scoring, traces
from evals.results import schema

__all__ = ["regression", "runner", "schema", "scoring", "traces"]
