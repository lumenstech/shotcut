# 0006 — Observability

**Status:** Accepted
**Date:** 2026-04-22
**Context:** Stage 9 of BUILD_PLAN.md — traces, metrics, cost.

## Decisions

### Tracing is pluggable; default is NoOp

`Tracer` is an ABC with three implementations:

- **`NoOpTracer`** — default. Spans are cheap no-op objects.
  Production deployments that haven't wired Langfuse yet run here
  without cost. CI runs here too: no side effects, no network.
- **`LangfuseTracer`** — production Langfuse backend. Lazy-imports
  the `langfuse` SDK so NoOp dev/test environments don't need it
  installed.
- **`RecordingTracer`** — tests. Collects every span into an
  in-memory list for assertion. Not a production backend.

`start_span(name, **attrs)` is a context manager that uses
`contextvars.ContextVar` to track the current parent span — nested
spans automatically chain without the caller threading references.
Works in both sync and async contexts (contextvars propagate across
`await`).

### Span taxonomy

Fixed four-level hierarchy — matches the BUILD_PLAN's "Session → Turn
→ Agent → LLM / Tool call" requirement:

| Level | Span name | Required attrs |
|---|---|---|
| Session root | `session.turn` | `session_id`, `tenant_id`, `user_sub` |
| Agent | `agent.<planner|executor|verifier|researcher>` | `step_title` (planner/executor only) |
| LLM call | `llm.messages.create` / `llm.messages.parse` | `model`, `input_tokens`, `output_tokens`, `cached_read_tokens`, `cost_usd` |
| Tool call | `tool.<name>` | `tool_name`, `tool_use_id` |

Adding a new span kind means adding a new well-known name; the tracer
doesn't care. Callers that don't supply required attrs get warnings
in the tracer-validation test, not runtime errors.

### Cost computation

Per-model pricing table in `observability/cost.py`, matching the
public cached-2026-04 rates (Opus 4.7 $5/$25 per 1M, Sonnet 4.6
$3/$15, Haiku 4.5 $1/$5). Cached-read tokens cost ~0.1× input;
cached-write ~1.25×. Function signature:

```python
compute_cost(model: str, input_tokens: int, output_tokens: int,
             cached_read: int = 0, cached_write: int = 0) -> float
```

Costs go onto every `llm.messages.*` span and into the
`shotcut_llm_cost_usd_total` counter. Tenant-level cost rolls up via
the counter's `tenant_id` label.

### Metrics via prometheus_client

Standard `prometheus_client` library. One module-level `CollectorRegistry`
so tests can snapshot values without racing with other test runs.

Core metric set (Stage 9):

| Metric | Type | Labels |
|---|---|---|
| `shotcut_requests_total` | Counter | `method`, `path`, `status` |
| `shotcut_request_duration_seconds` | Histogram | `method`, `path` |
| `shotcut_agent_step_duration_seconds` | Histogram | `agent` |
| `shotcut_agent_errors_total` | Counter | `agent` |
| `shotcut_llm_tokens_total` | Counter | `model`, `kind` |
| `shotcut_llm_cost_usd_total` | Counter | `model`, `tenant_id` |

`GET /metrics` serves the Prometheus text format. It's unauthenticated
because Prometheus scrapers need network-level trust, not JWT auth —
the endpoint is always mounted inside a network boundary.

### Wiring point is `llm/client.py`

`traced_create(client, **kwargs)` and `traced_parse(client, **kwargs)`
helpers wrap `messages.create` and `messages.parse` with span +
metric emission. Every agent that makes Anthropic calls routes
through these helpers; one patch point.

The alternative (subclassing `AsyncAnthropic`) would be zero-caller-
change but has the disadvantage that Anthropic SDK calls made outside
the agent boundary (e.g. in tests) would also emit spans, polluting
the trace tree. Explicit helpers make the observability surface
greppable.

### Deferred to post-Stage-9

- **Grafana dashboard JSON.** The metrics exist; turning them into
  a dashboard is a deployment concern, not code.
- **`docker-compose.observability.yml`.** Langfuse self-host needs
  Postgres + Redis + the Langfuse containers. Straightforward but
  out of scope for the backend MVP.
- **Custom eval registration.** The BUILD_PLAN called out
  `formula_accuracy`, `verification_caught_error`, `action_succeeded`.
  Eval registration needs a running Langfuse instance and is natural
  to pair with Stage 10's eval suite.
- **Per-tenant cost reports / budgets.** The `shotcut_llm_cost_usd_total`
  counter is labeled by tenant; deriving monthly reports or enforcing
  budget caps is a query-side concern we'd build when it becomes
  real.
