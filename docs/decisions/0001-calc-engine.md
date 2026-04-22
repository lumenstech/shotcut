# 0001 — Calculation Engine

**Status:** Accepted
**Date:** 2026-04-22
**Context:** Stage 1 of BUILD_PLAN.md

## Decision

Use `formualizer` (Rust core, PyO3 bindings, v0.5.6+) as the formula evaluation engine.

## Probe summary

Clean `python3 -m venv` on Ubuntu 24.04, Python 3.11.15.

| Check | Result |
|---|---|
| `pip install formualizer` | Pre-built wheel `formualizer-0.5.6-cp310-abi3-manylinux_2_17_x86_64` — 6.8 MB, installs in <1s, no Rust compile |
| Python ABI | `cp310-abi3` (stable ABI, 3.10+) — same wheel serves 3.11 and 3.12 |
| Rust toolchain needed? | No, as long as the wheel exists. (Host has `rustc 1.94.1` as a fallback.) |
| `=SUM(A1:A5)` over 1..5 | 15.0 ✓ |
| `=INDEX(B1:B3, MATCH("beta", A1:A3, 0))` | 20.0 ✓ |
| Cross-sheet: `=Inputs!A1 * 2` | 200.0 ✓ |
| Named range via `load_workbook(path)` | `=SUM(MyRange)` → 60.0 ✓ |
| Cycle detection: A1=B1, B1=A1 | Returns `#CIRC!` error value rather than raising |

Stage 1 function-list coverage: **18/18**. All of SUM, AVERAGE, MIN, MAX, COUNT, COUNTA, IF, IFS, AND, OR, NOT, INDEX, MATCH, VLOOKUP, XLOOKUP, TODAY, EDATE, EOMONTH evaluate to the expected values.

## Trade-offs and quirks

**Adopting formualizer means:**
- Pre-built manylinux + macOS + Windows wheels — no build-from-source burden in CI
- Rust-speed evaluation; built-in dependency graph; changelog/undo primitives we can reuse in Stage 5
- Native Excel semantics for errors (`#DIV/0!`, `#REF!`, `#CIRC!`, `#VALUE!`) returned as `{type: "Error", kind: ...}` dicts instead of Python exceptions

**Known quirks the engine layer must paper over:**

1. **Cycle detection returns a value, not an exception.** `evaluate_cell` on a cycle returns `{'type': 'Error', 'kind': 'Circ'}`. The `Engine` wrapper raises `CircularReferenceError` when it sees that shape. Same treatment for other `#REF!`, `#NAME?`, `#VALUE!` — surface them to the verifier as findings rather than silent values.

2. **`TODAY()` returns 25569 (1970-01-01) by default, not the host's current date.** Formualizer pins the clock for determinism; there is no `today_override` property on `EvaluationConfig`. Workarounds, in order of preference:
   - Write a real date value to a pinned cell at session start and have the planner prompt the executor to reference it instead of calling `TODAY()` directly.
   - Ship a thin function shim in the engine wrapper that intercepts `TODAY`/`NOW` during parse and rewrites them to literal serials.
   - Defer to upstream — file an issue asking for a clock knob.
   Deciding in Stage 1 implementation; leaning toward (1) because it's the pattern finance modelers already use ("Valuation Date" input cell).

3. **No programmatic `set_named_range` in the Python bindings.** Named ranges are read via `get_named_ranges()` when a workbook is loaded from an `.xlsx`, but there's no setter. For now the executor does not create named ranges (it uses absolute refs); when Stage 2 parses uploads with existing names, those names flow through unchanged. If Stage 4's verifier wants to suggest "promote this range to a name," we route that through openpyxl's `DefinedName` and reload, rather than going through formualizer.

4. **1-based row/col indexing** on the Python API (as in Excel). The existing `openpyxl`-based `Workbook` class uses A1 strings everywhere; the engine wrapper converts at the seam.

## Alternatives considered

| Option | Why not |
|---|---|
| `xlcalculator` | Pure-Python, broad function coverage, but 20–200× slower on large workbooks and its dependency graph is naive (no incremental recalc). Kept as a fallback if a formualizer wheel ever breaks CI. |
| `pycel` | Originally named in the build plan as the fallback. Narrower function coverage than `xlcalculator`, same pure-Python speed penalty. Superseded by `xlcalculator` as the de-facto fallback. |
| Hand-rolled evaluator over openpyxl's tokenizer | Viable for the ~50 functions we actually use, but reinvents the dependency graph, cycle detection, and error-propagation work formualizer already does. Worth ~2 weeks that Stage 1 doesn't have. |
| Run LibreOffice headless and re-open | Too slow for inline evaluation; reserved for fidelity-test comparisons in Stage 10. |

## Fallback plan

If a future formualizer release stops shipping wheels for our target Python ABI, or a critical bug blocks us:

1. Pin to the last working `formualizer` version in `pyproject.toml`.
2. If that's untenable, switch the `Engine` wrapper to delegate to `xlcalculator`. The wrapper's public surface (`evaluate_cell`, `evaluate_all`, `CircularReferenceError`, `UnsupportedFormulaError`) is designed to be engine-agnostic so the swap is one implementation file.

## Follow-ups for Stage 1 implementation

- Wrap `formualizer.Workbook` behind a `shotcut.spreadsheet.engine.Engine` class that:
  - Accepts our existing `Workbook` (openpyxl-backed) and mirrors its state
  - Translates A1 refs ↔ (sheet, row, col) tuples at the boundary
  - Converts `#CIRC!`, `#REF!`, etc. into typed exceptions
  - Handles the `TODAY()` quirk per the decision above
- Add `tests/fixtures/formulas/excel_truth.json` with the 20 ground-truth formulas. Generate the expected values from actual Excel (or LibreOffice headless as a proxy) so the CI can verify against a real engine.
