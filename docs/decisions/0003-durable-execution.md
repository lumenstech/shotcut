# 0003 — Durable Execution

**Status:** Accepted
**Date:** 2026-04-22
**Context:** Stage 6 of BUILD_PLAN.md.

## Decision

Stage 6 makes orchestrator runs durable (survive worker restarts) and
observable (SSE progress events) without introducing a workflow engine
(Temporal, Cadence). The machinery is deliberately small: a Postgres
state table, a per-session asyncio queue, and a startup resume scan.

## Why not Temporal

For the workbook sizes and turn frequencies we target — minutes-long
turns, dozens to low-hundreds per user per day — Temporal is overkill.
Running a workflow engine means:
- A second datastore to operate (Temporal backend)
- A second coordination model (workflow definitions, activities,
  deterministic replay semantics) to learn
- Additional failure modes (Temporal cluster health, workflow history
  compaction)

The workflow we actually need is linear: `PLAN → (each step: EXECUTE)
→ VERIFY → DONE`. Postgres-backed state-machine checkpointing covers
it with an order of magnitude less complexity. When turns grow to
tens of minutes, touch multiple services, or need fan-out across
workers, revisit — by then a migration to Temporal is a contained
refactor of `orchestrator/durable.py` and not an architectural redo.

## State machine

```
 PLANNING ──► EXECUTING ──► VERIFYING ──► DONE
    │            │              │
    └──┬─────────┴──────────────┘
       ▼
     FAILED
```

`PENDING_APPROVAL` actions from Stage 3 and staged approvals are
orthogonal to this state machine — they live on `actions.status`, not
on the orchestrator's per-run state.

## Checkpoint granularity

**Per plan step, not per action.** When the orchestrator finishes
applying every action for step K, it:

1. Writes every action through `audit.record()` inside the same DB
   transaction it bumps `orchestrator_states.current_step_index` in.
2. Commits.

Either both hit Postgres or neither does. On resume, the orchestrator
reads `current_step_index` and skips steps it has already completed.

Per-action checkpointing would be safer mid-step (recover the last
half of a 50-cell formula write), but it doubles our commit count for
modest safety gains. Stage 6 MVP accepts losing mid-step progress on a
crash; steps are idempotent at their boundaries.

## Event bus

In-process `asyncio.Queue` per session. The durable orchestrator
emits `ProgressEvent` objects into the queue; the SSE handler pulls
from it and streams to the client. When the session reaches a
terminal state, the handler drains and closes.

**Upgrade path to Redis pub/sub.** Multi-worker deployments break the
in-process bus: the SSE handler might be served by a worker that
isn't running the orchestrator. Swap the bus to Redis pub/sub channel
per session — the orchestrator `PUBLISH`es, the SSE handler
`SUBSCRIBE`s. Same `ProgressEvent` shape, different transport. Wait
for multi-worker deployment to do it.

## Worker-boot resume

On startup, scan `orchestrator_states` for rows whose `state` is not
terminal (`DONE` or `FAILED`) and no orchestrator task is currently
owning them in-process. For each, spawn a background task that calls
`durable.resume(session_id)`. The task picks up at
`current_step_index` and continues.

**Double-execution guard.** The state table has no locking column in
the MVP — two workers coming up simultaneously might both claim the
same session. Acceptable for single-worker Stage 6; multi-worker
setups will need either advisory locks or an `owner_worker_id`
column. Documented here as a follow-up.

## Idempotence guarantees

- Re-running the orchestrator from a checkpoint produces the same
  `audit.actions` rows as if it had never been interrupted: the audit
  log is the source of truth for "what happened."
- Repeated resume of the same non-terminal session is safe —
  `current_step_index` advances monotonically; step executions for
  indices less than the current don't re-run.
- Failed resume (exception mid-step) does NOT advance the index.
  Another resume will retry the same step.

## `Workbook.with_actions_applied`

Stage 4's verifier clones the workbook via BytesIO round-trip to
evaluate pending-approval actions without mutating the persisted
state. Stage 6 adds `Workbook.with_actions_applied(actions)` as a
context manager so callers have a single named shape for "give me a
workbook that looks like these actions were applied."

Current implementation: full clone (same cost as Stage 4). The API
surface is ready for a copy-on-write implementation when profiles
show it matters — the public contract (`with ... as clone: ...`)
doesn't change.

## What Stage 6 does NOT introduce

- **No workflow engine.** Postgres is enough.
- **No Redis.** In-process asyncio queue for the MVP; Redis is the
  documented upgrade when deployment fans out.
- **No per-action checkpointing.** Per-step is sufficient.
- **No cross-worker claim tracking.** Single-worker MVP; advisory
  locks land with multi-worker deployment.
- **No LLM-response caching key in the checkpoint.** The original
  spec mentioned `last_llm_response_id` for prompt-cache reuse on
  resume; deferred — Anthropic's prompt cache is effective at the
  breakpoint level without explicit id reuse, and wiring it adds
  surface for marginal benefit at Stage 6 turn volumes.
