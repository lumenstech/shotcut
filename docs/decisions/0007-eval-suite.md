# 0007 — Eval Suite

**Status:** Accepted
**Date:** 2026-04-22
**Context:** Stage 10 of BUILD_PLAN.md — catch regressions, measure
progress against SOTA, flow every invocation through Stage 9's
observability stack so failures are *diagnosable*, not just countable.

## Decisions

### Framework first; full SpreadsheetBench dataset is deployment work

Stage 10 ships the evaluator framework (runner, scoring, baseline
comparison, trace tagging, report format) plus a committed golden
set of hand-curated cases that run in CI. Actually downloading and
running the full 912-case SpreadsheetBench dataset requires:

- a large dataset download that CI shouldn't do on every push,
- Anthropic API credit burn we're not going to pay in CI,
- potentially hours of wall-clock time.

So the SpreadsheetBench runner is a shell that accepts a dataset path
+ subset filter; production triggers it on a weekly cron. Tests run
against a tiny fixture dataset that exercises every code path without
hitting Anthropic.

### Every eval invocation flows through the Stage 9 tracer

`EvalContext` opens a `session.turn` span with:

- `trigger = "eval"` (distinguishes from `trigger = "human"` on
  user-driven turns — production's Langfuse dashboards filter cleanly)
- `eval_suite = "golden" | "spreadsheetbench"`
- `eval_case_id = <stable case identifier>`

The root span's id becomes the `langfuse_trace_id` field on every
result row, so a failed case's report entry is clickable. Under the
NoOp tracer (CI default), the trace id is a locally-generated UUID
that doesn't resolve anywhere — the field is populated but inert.

### Scoring is cell-by-cell + formula-aware + structural

A scorer compares expected vs actual workbook on three axes, returning
per-axis verdicts so the report says *what* differed, not just
"failed":

- **Cell values**: exact match on numeric (tolerance 1e-6) / string /
  boolean. Missing-vs-extra cells reported separately.
- **Formulas**: compare formula strings verbatim for cells that
  expected one. Also compare evaluated values via the Stage 1 engine;
  a cell that has a different formula but the same evaluated value is
  labeled `formula_drift` (warn), not `value_mismatch` (fail).
- **Structure**: sheet names + ordering + row/column dimensions.
  Extra/missing sheets are a value-mismatch-level failure.

Cell-by-cell diffs are capped at the first 20 per case; the counter
records the true total.

### Baseline file is never auto-overwritten

`evals/results/baseline.json` is committed, human-edited only. The
CI regression gate reads it and compares. Passing runs don't write
it back; a floor-raise requires an explicit commit with a message
explaining why. This is the single most important rule of the suite —
auto-updating baselines on green runs is how eval suites become
silent-regression-accepters.

The baseline stores per-case verdict (not aggregate pass rate) so
SpreadsheetBench regressions are traceable to specific cases, and
golden-set 100% failures point at a named model.

### Regression gate rules

Two gates, run on every PR:

1. **Golden set**: must pass 100%. A single failure fails the PR.
2. **SpreadsheetBench**: must not drop more than 2% from the
   baseline's aggregate pass rate. The 2% window is deliberately
   asymmetric — we allow runs to improve freely, but require a
   conscious commit to ratchet the baseline up.

The gate runs a small subset in CI (< 1 minute). Full SpreadsheetBench
runs weekly via cron and produces a dashboard report rather than a CI
gate (cost + time).

### What the failure report looks like

Per case (Pydantic shape, one row per case):

```
{
  "case_id": "dcf_three_year",
  "suite": "golden",
  "verdict": "failed",
  "langfuse_trace_id": "abc123...",
  "duration_seconds": 4.2,
  "diffs": [
    {"axis": "cell_value", "location": "Model!B5",
     "expected": 1000.0, "actual": 1250.0},
    {"axis": "formula", "location": "Model!B7",
     "expected": "=B5*B6", "actual": "=1250*B6"}
  ],
  "diffs_truncated": false,
  "message": "2 cell-value mismatches, 1 formula drift"
}
```

The per-case trace URL is what makes this actionable: open the trace,
see which verifier finding fired, which executor-emitted action
produced the wrong cell, what prompt the planner used.

## What Stage 10 does NOT introduce

- **Full SpreadsheetBench dataset bundling**: framework only.
  Production downloads via weekly cron to a shared storage location.
- **Grafana dashboards for eval metrics**: the trace tags make them
  implementable; we ship the data, not the viz.
- **Automatic baseline-update flow**: explicitly out. Baselines are
  human-committed.
- **Flaky-test tolerance / retry loops**: tests that rely on Anthropic
  are offline-mocked; there's nothing to be flaky about until we wire
  a live-Anthropic CI job (which we won't).

## Follow-up: recorded-trace producer (shipped)

The initial Stage 10 scaffold used an identity producer — each case's
prompt mapped directly to its expected workbook, exercising the
framework (traces, scoring, gate) but not any agent machinery. The
follow-up replaces that with a cassette-based replay producer:

- `evals/recording/cassettes/<case_id>.json` stores the Plan + per-
  step executor action list + VerificationReport that the agents
  *would have* produced for the case's prompt.
- `evals/recording/producer.py`'s `recorded_trace_producer(case_id)`
  loads the cassette at closure-build time (so `CassetteMissing`
  fires at CLI startup, not mid-run) and applies the recorded
  actions against `Workbook.blank()`.
- The CLI's golden run uses the recorded-trace producer by default.
  Missing cassettes land as `verdict=errored` rows — the gate fails
  loudly on drift between `golden/cases.py` and the cassette
  directory.

What this catches that the identity producer didn't:

- Action schema drift: cassettes fail to load when the Pydantic
  discriminated union rejects them. A new required field on
  `WriteFormula`, a renamed action type, etc. trip at load time.
- `Workbook.apply` regressions: cassettes that used to produce the
  expected workbook now produce a different one → scorer diff.
- Scorer regressions: identical cassettes + workbooks produce
  different diff-labeling.

What it still doesn't catch: the Anthropic SDK layer (covered by
`tests/observability/test_llm_wiring.py`), agent prompt stability
(inherently non-deterministic, not something to verbatim-gate on),
and full orchestrator behavior including DB audit. A richer
follow-up would wire `orchestrator.run()` with stubbed agents
reading from the cassette; scoped out for now because the apply-
path coverage here is already a meaningful step up from identity.

**Cassette update workflow.** When an action schema changes or a
golden case's expected workbook moves, the cassette needs to move
with it. Workflow:

1. Run the real agents against the case's prompt in a scratch
   environment (live Anthropic).
2. Capture the Plan + executor outputs + verifier report.
3. Serialize to the cassette JSON schema.
4. Commit the new cassette alongside whatever change motivated it.

We don't ship tooling for (1)-(3) in this follow-up; cassettes are
small enough today to hand-craft, and the failure mode (scorer diff
with clear location) points at what needs updating.
