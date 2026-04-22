# 0002 — Schema Consolidation

**Status:** Accepted
**Date:** 2026-04-22
**Context:** Stage 3 of BUILD_PLAN.md, forward-looking to Stages 5 and 8.

## Decision

Ship every schema change Stages 3, 5, and 8 will need as a single Alembic
migration at the start of Stage 3. Later stages add behavior against
columns that already exist, rather than adding columns of their own.

This is a deliberate departure from the BUILD_PLAN's per-stage migration
convention. The motivation is that the audit/action table is the central
index of the whole system; three separate migrations against it — each
touching the same rows, each requiring test-data rewrites — would churn
more than the consolidation costs.

## Final schema

Applied in migration `0001_consolidate_schema.py`.

### `sessions` table

| Column | Type | Nullable | Added in | Used by |
|---|---|---|---|---|
| `id` | UUID | No | Stage 0 | everywhere |
| `title` | String(256) | Yes | Stage 0 | UI |
| `workbook_path` | String(512) | No | Stage 0 | orchestrator, download |
| `original_workbook_path` | String(512) | Yes | Stage 2 | Stage 3 occupancy, Stage 5 replay |
| `tenant_id` | UUID | Yes (populated in Stage 8) | **Stage 3 migration** | Stage 8 RLS |
| `created_at` / `updated_at` | TimestampTZ | No | Stage 0 | observability |

Index: `ix_sessions_original_workbook_path` for Stage 5 replay reconstruction.
Index: `ix_sessions_tenant_id` — dormant until Stage 8 populates the column.

### `actions` table

| Column | Type | Nullable | Added in | Used by |
|---|---|---|---|---|
| `id` | UUID | No | Stage 0 | everywhere |
| `session_id` | UUID FK → sessions | No | Stage 0 | audit lookup |
| `sequence` | int | No | Stage 0 | ordering |
| `agent` | String(64) | No | Stage 0 | attribution |
| `action_type` | String(64) | No | Stage 0 | dispatch |
| `sheet` / `target_range` | String | Yes | Stage 0 | audit readback |
| `previous_value` / `new_value` | JSON | Yes | Stage 0 | undo/replay |
| `reasoning` | Text | Yes | Stage 0 | explainability |
| `created_at` | TimestampTZ | No | Stage 0 | audit timeline |
| `parent_action_id` | UUID FK → actions | Yes | **Stage 3 migration** | Stage 5 branching |
| `status` | Enum(applied, pending_approval, rejected, undone) | No | **Stage 3 migration** | Stages 3 + 5 |
| `approval_required_reason` | Text | Yes | **Stage 3 migration** | Stage 3 approval flow |
| `force_override` | bool | No, default False | **Stage 3 migration** | Stage 3 occupancy override |
| `tenant_id` | UUID | Yes | **Stage 3 migration** | Stage 8 RLS |
| `user_sub` | String(256) | Yes | **Stage 3 migration** | Stage 8 audit (JWT subject) |

Index: `ix_actions_parent_action_id` for Stage 5 tree walks.
Index: `ix_actions_status` — approve-endpoint and pending-list queries.
Index: `ix_actions_tenant_id` — dormant until Stage 8.

### Action status state machine

```
               ┌──────────────────────────┐
               │      pending_approval    │◄──┐
               └──────────┬───────────────┘   │
                          │                   │
              ┌───────────┼───────────┐       │  (orchestrator stages
              │           │           │       │   an occupancy-blocked
              ▼           ▼           ▼       │   action — Stage 3)
         approve       reject      (Stage 5
              │           │        branching)
              ▼           ▼           │
        ┌──────────┐ ┌──────────┐     │
        │ applied  │ │ rejected │     │
        └─────┬────┘ └──────────┘     │
              │ (Stage 5 undo)        │
              ▼                       │
        ┌──────────┐                  │
        │  undone  │                  │
        └──────────┘                  │
```

`rejected` and `undone` are terminal — once set, they are never revisited.
A new action with a new `id` is created for reattempts.

## Why lock the shape now

1. **The audit table is a hot path.** Alembic-migrating `actions` with
   even a few thousand rows requires a strategy (backfill, batching,
   dual-write). Doing that once is fine; doing it three times — at
   Stages 3, 5, and 8 — triples the risk surface on a table we rely on
   for every debugging and replay operation.
2. **Stage 8 RLS policies key on `tenant_id`.** Adding an RLS policy
   requires either the column to already exist (our choice) or a
   migration that adds the column *and* the policy in one atomic step
   (harder to get right and test). Nullable now, populated later.
3. **Stage 5's branching model (`parent_action_id`) informs API
   contracts we're writing in Stage 3.** If we designed Stage 3's
   approval flow without knowing the parent chain exists, the approve
   endpoint would need a breaking change in Stage 5 to record that an
   approved action is a child of the pending one, not a sibling.

## What Stage 3 uses

- `force_override` — the approval endpoint sets this to True when applying
  a previously-pending action.
- `status` — `applied` is the default for occupancy-clean actions;
  `pending_approval` is set when the executor proposes a write to a
  user-occupied cell.
- `approval_required_reason` — populated with a human-readable reason
  ("would overwrite user-sourced cell Inputs!B3 containing value 1000").

## What Stage 5 will add (code only, no migration)

- Populate `parent_action_id` when `branch` operations fork a session.
- Set `status = 'undone'` on inverse application instead of deleting rows.
- Use `original_workbook_path` index for fast replay bootstrap.

## What Stage 8 will add (code + policy, no column migration)

- Populate `tenant_id` on every write from the authenticated request
  context.
- Populate `user_sub` from JWT `sub` claim.
- Create Postgres RLS policies on `sessions` and `actions` keyed on
  `current_setting('app.tenant_id')`.

## Risks accepted

- **Unused columns ship early.** `tenant_id`, `user_sub`,
  `parent_action_id` are nullable no-ops until their stages. Stage 3/4
  queries must `SELECT` them without relying on values. Cost: a handful
  of bytes per row that we'd pay anyway at Stage 8.
- **Enum extension later is a Postgres `ALTER TYPE`.** If Stage 5
  adds a new status value (e.g. `superseded`), that's a single
  migration. Not free, but bounded and local to the enum.
- **A future stage discovers a needed column we didn't foresee.** Then
  we add it in a narrow migration at that stage. This decision isn't
  "no more migrations ever" — it's "consolidate the foreseeable ones
  into Stage 3."

## Alembic setup

This stage establishes Alembic as the migration tool. `main.py`'s
`Base.metadata.create_all` call is retained as a dev-only convenience
for empty databases — in production, `alembic upgrade head` runs first
and `create_all` is a no-op. Subsequent stages' migrations live in
`alembic/versions/` and are numbered in order (`0002_...`, `0003_...`).

## Stage 4 addendum — verifier short-circuit and cloning

**Deterministic levels always run; the LLM level is gated.** Stage 4
originally specified "five levels, run in order, short-circuit on
critical failure." As shipped, the four deterministic levels (syntax,
reference, cycle, numerical) always run across every formula cell and
produce the complete findings set. Only the semantic (LLM-billed) level
is gated — it runs when no deterministic finding is `CRITICAL`, is
skipped otherwise. This gives the user complete debug surface in one
response, lowers cost, and doesn't require re-running verification to
see everything that's wrong. The original "first-finding short-circuit"
reading is explicitly rejected. Future stages reference this clause.

**`#NAME?` classifies as REFERENCE, not SYNTAX.** From the user's
perspective a typoed function name and an unresolved named range are
the same failure class: something that should resolve didn't. The
verifier treats both as `REFERENCE` findings at `WARNING` severity so
the model can suggest replacements rather than blocking.

### Cloning for pending-approval verification

The verifier evaluates the workbook *as if* all `pending_approval`
actions in the current turn were applied, without mutating the
persisted workbook. Two viable shapes:

1. **Cheap clone (adopted).** Serialize the current workbook to
   `BytesIO`, deserialize into a fresh `Workbook`, apply each pending
   action against the clone, run verifier levels over the clone, let
   GC reclaim it at the end of the turn. Works today, no new API
   surface; each verification pass costs one extra openpyxl
   round-trip (tens of milliseconds for workbooks in the kilocell
   range, which matches our target).

2. **Copy-on-write context manager (Stage 6 upgrade).** Add
   `Workbook.with_actions_applied(actions: list[Action]) -> Workbook`
   as a context manager that materializes a shadow state only for
   cells touched by the supplied actions, backed by the original
   workbook for everything else. Stage 6's checkpointing / replay
   wants this: replaying a turn's pending state against a large
   historical workbook shouldn't pay a full clone cost every time,
   and the shadow can be discarded atomically on session rollback.

Decision: ship (1) in Stage 4. Note (2) here as the eventual Stage 6
follow-up; it becomes worth implementing once checkpoint replay makes
the clone cost visible in profiles. The verifier's public surface
(`verify(workbook, pending_actions, ...)`) is stable across the
upgrade — only the Workbook layer changes.

## Stage 5 erratum — `client_action_id`

**One-time erratum to the Stage 3 consolidation.** Stage 3 locked the
`actions` schema under the "Stages 5/8 add behavior, never columns"
rule. Stage 4's finding-attribution work surfaced a gap: the
verifier's `attribute_to_actions` originally keyed on `id(action)` —
Python's `id()`, pointer-stable only within a process. Stage 5's
branching (same logical action copied into a new branch with a new
DB row) and Stage 6's checkpointing (action objects round-tripping
through pickle across worker boundaries) both break that key silently.

Fix shipped at the start of Stage 5, in migration
`0002_client_action_id.py`:

- `Action.client_action_id: UUID NOT NULL` — generated by the domain
  `Action` Pydantic model at emission time (`default_factory=uuid4`),
  preserved across serialization and branch copy. It's the stable
  identity of a logical action across processes, replays, and branches.
- `actions.parent_action_id` (already present from Stage 3) chains
  within a branch: "this action came after that action in sequence."
  `client_action_id` chains across branches: "this is the same logical
  action in a different branch."
- Migration runs `ADD COLUMN client_action_id UUID` (nullable) →
  backfill every existing row with `uuid4()` → `ALTER COLUMN SET NOT
  NULL`. Runs cleanly on a fresh DB (no rows to backfill) and on a
  Stage-3/4-populated DB alike.

**The erratum clause is single-use.** `client_action_id` is the only
column retroactively added to a Stage 3 table after Stage 3 ship. No
further additions to `actions` or `sessions` under the erratum name.
Legitimate new-feature tables (e.g. `workbook_snapshots`,
`session_branches`) remain fair game because they're new-capability
additions, not retroactive patches — they don't disrupt the RLS
policies Stage 8 will attach to the existing tenant-scoped tables.
