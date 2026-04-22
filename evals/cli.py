"""`python -m evals.cli <suite> [--gate]` — thin CLI around the runner.

Invoked by `.github/workflows/evals.yml`. Two modes:

  - `golden` — runs the committed golden set via a deterministic
    test producer (no Anthropic calls). Pass with `--gate` to enforce
    the regression rule (`GoldenFailure` → exit 1).
  - `spreadsheetbench` — runs a dataset manifest from
    `SPREADSHEETBENCH_DATASET` (default: built-in test fixture) with a
    subset size from `SPREADSHEETBENCH_MAX_CASES`. Also supports `--gate`.

The CLI is intentionally thin — the heavy lifting lives in
`evals.runner` / `evals.regression`. This module exists so CI has a
stable invocation point.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from evals import regression
from evals.golden.cases import CASES as GOLDEN_CASES
from evals.results.schema import SuiteResult
from evals.runner import EvalCase, Producer, run_suite
from evals.spreadsheet_bench import runner as sb_runner
from shotcut.spreadsheet.workbook import Workbook


def _identity_producer_for(cases: list[EvalCase]) -> Producer:
    """Build a prompt-keyed producer that returns each case's expected
    workbook.

    This is the MVP scaffold's deterministic producer: it exercises the
    framework end-to-end (traces, scoring, regression gate) on every
    committed golden case without hitting Anthropic. The cases pass
    trivially because the actual workbook is the expected workbook —
    so CI protects baseline-drift (a case disappearing, a prompt
    changing) without protecting agent output (which needs a recorded-
    trace producer, a Stage 10+ follow-up).
    """
    prompt_to_expected = {c.prompt: c.expected for c in cases}

    async def producer(prompt: str) -> Workbook:
        if prompt in prompt_to_expected:
            return prompt_to_expected[prompt]
        # Unknown prompt (e.g. a SpreadsheetBench case without a
        # recorded trace): return an empty workbook so the case fails
        # scoring loudly instead of silently passing.
        from openpyxl import Workbook as OpenpyxlWorkbook

        return Workbook(OpenpyxlWorkbook())

    return producer


async def _run_golden() -> SuiteResult:
    producer = _identity_producer_for(GOLDEN_CASES)
    results = await run_suite(
        GOLDEN_CASES, suite="golden", producer=producer
    )
    return SuiteResult.from_cases(suite="golden", cases=results)


async def _run_spreadsheetbench() -> SuiteResult:
    dataset_root = Path(
        os.environ.get(
            "SPREADSHEETBENCH_DATASET",
            str(Path(__file__).parent / "spreadsheet_bench" / "test_fixture"),
        )
    )
    max_cases_raw = os.environ.get("SPREADSHEETBENCH_MAX_CASES")
    max_cases = int(max_cases_raw) if max_cases_raw else None
    filt = sb_runner.DatasetFilter(max_cases=max_cases)
    if not dataset_root.exists():
        # CI without a bundled dataset: return an empty suite so the
        # regression gate treats it as "nothing ran" (no verdict).
        return SuiteResult.from_cases(suite="spreadsheetbench", cases=[])
    return await sb_runner.run(
        dataset_root=dataset_root,
        producer=_identity_producer_for([]),  # no cases pre-loaded
        filter=filt,
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="evals.cli")
    parser.add_argument(
        "suite",
        choices=["golden", "spreadsheetbench"],
        help="which suite to run",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="enforce baseline rules; exit non-zero on regression",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print full results as JSON on stdout",
    )
    args = parser.parse_args()

    if args.suite == "golden":
        result = asyncio.run(_run_golden())
        report = regression.evaluate(golden=result)
    else:
        result = asyncio.run(_run_spreadsheetbench())
        report = regression.evaluate(spreadsheetbench=result)

    if args.json:
        print(json.dumps(result.model_dump(mode="json"), indent=2))
    else:
        print(
            f"{result.suite}: {result.passed}/{result.total} passed "
            f"({result.failed} failed, {result.errored} errored)"
        )
        print(f"gate: {report.message}")

    if args.gate and not report.passed:
        # Emit failing case details to stderr for human diagnosis.
        for case in (result.cases if result else []):
            if case.verdict != "passed":
                print(
                    f"  FAILED {case.case_id}: {case.message or '(no message)'} "
                    f"[trace={case.langfuse_trace_id}]",
                    file=sys.stderr,
                )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
