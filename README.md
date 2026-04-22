# Shotcut

Multi-agent spreadsheet intelligence backend — an open-source reimplementation of the Shortcut.ai architecture as a shallow MVP.

Given a natural-language prompt and an optional `.xlsx`, the backend runs a planner → executor → verifier agent pipeline built on the Anthropic SDK and returns a formula-driven workbook plus a cell-level audit log.

## Architecture

```
User prompt + optional .xlsx
          │
          ▼
┌───────────────────────────────┐
│  FastAPI (src/shotcut/api)    │
│  POST /sessions               │
│  POST /sessions/{id}/prompt   │
│  GET  /sessions/{id}/workbook │
│  GET  /sessions/{id}/audit    │
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│  Orchestrator                 │
│  (agents/orchestrator.py)     │
│                               │
│  plan → [execute] → verify    │
└──────────────┬────────────────┘
               │
   ┌───────────┼──────────────┬────────────┐
   ▼           ▼              ▼            ▼
Planner    Executor       Verifier    Researcher
(parse)    (tool use)     (parse)     (edgartools)
  │           │              │            │
  └───────────┴──────────────┴────────────┘
               │
               ▼
┌───────────────────────────────┐
│  Workbook (openpyxl)          │
│  + formula validation engine  │
│  + cell-level diff            │
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│  Postgres audit log           │
│  (one row per Action)         │
└───────────────────────────────┘
```

### Agents

| Agent | Responsibility | Claude surface |
|---|---|---|
| Planner | Decompose prompt into ordered, bounded steps | `messages.parse()` with Pydantic `Plan` schema |
| Executor | Per step: emit `Action` objects (write formula / value / format) | Manual tool-use loop with five spreadsheet tools |
| Verifier | Find syntactic + semantic issues | Deterministic formula checker + `messages.parse()` for semantics |
| Researcher | Fetch live data from SEC filings | `messages.parse()` over edgartools output |

All agents default to `claude-opus-4-7` with adaptive thinking and `effort: "high"`. Researcher uses `claude-haiku-4-5` (cheap lookups). Override via env vars — see `.env.example`.

## Quickstart

```bash
# 1. Start Postgres
docker compose up -d postgres

# 2. Install
pip install -e ".[dev]"

# 3. Configure
cp .env.example .env
# edit .env: set ANTHROPIC_API_KEY

# 4. Run
uvicorn shotcut.main:app --reload

# 5. Try it
curl -X POST http://localhost:8000/sessions
# → { "id": "...", "created_at": "..." }

curl -X POST http://localhost:8000/sessions/$SESSION_ID/prompt \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Build a 3-year revenue model for a SaaS company with 10% monthly growth"}'

curl -o out.xlsx http://localhost:8000/sessions/$SESSION_ID/workbook
```

## What's in scope / not in scope

**In scope (MVP):**
- Planner / executor / verifier agent loop, each using the Anthropic SDK directly
- openpyxl-backed workbook manipulation with Action-level audit logging
- Formula syntax validation (balanced parens, known function names)
- SEC EDGAR integration via edgartools
- FastAPI endpoints for session lifecycle + workbook download

**Out of scope (explicit gaps):**
- **Formula evaluation.** openpyxl doesn't evaluate formulas; we rely on Excel to recalculate on open. A real implementation would wrap Formualizer or HyperFormula — see `spreadsheet/engine.py`.
- **Non-destructive diff engine.** When the user uploads an existing workbook, we currently overwrite cells directly. A production diff would detect empty slots and refuse overlapping writes.
- **Streaming progress.** The prompt endpoint blocks until the full pipeline completes. SSE or WebSocket streaming of plan/execute/verify events is the next step.
- **Durable workflow execution.** The orchestrator is a single async function; it doesn't survive a restart mid-run. Wrap in Temporal or Celery for real workloads.
- **Observability.** No Langfuse / Phoenix integration. Every agent call is unobserved beyond Python logging.
- **Auth.** No tenant isolation or API keys; every route is open.

## Layout

```
src/shotcut/
├── main.py            # FastAPI app + startup
├── config.py          # pydantic-settings
├── api/
│   ├── routes.py      # endpoints
│   └── schemas.py     # request/response models
├── agents/
│   ├── orchestrator.py  # plan → execute → verify state machine
│   ├── planner.py
│   ├── executor.py
│   ├── verifier.py
│   └── researcher.py
├── spreadsheet/
│   ├── workbook.py    # openpyxl wrapper, applies Actions
│   ├── actions.py     # discriminated union of mutation types
│   ├── engine.py      # formula syntax validator
│   └── diff.py        # before/after cell diff
├── data/edgar.py      # edgartools wrapper
├── db/
│   ├── models.py      # SQLAlchemy (sessions, actions)
│   ├── session.py
│   └── audit.py       # record() helper
└── llm/client.py      # shared AsyncAnthropic instance
```

## Tests

```bash
pytest
```

The smoke tests (`tests/test_workbook.py`, `tests/test_engine.py`) don't hit the network — they verify the workbook apply/reload round-trip and the formula validator. Agent-level tests would need either recorded fixtures or live API keys; neither is in scope for this MVP.
