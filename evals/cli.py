"""`python -m evals.cli <suite> [--gate]` — thin CLI around the runner.

Invoked by `.github/workflows/evals.yml`. Two modes:

  - `golden` — runs the committed golden set via the recorded-trace
    producer (loads cassettes from `evals/recording/cassettes/`).
    Missing cassettes are a hard error: drift between `golden/cases.py`
    and the cassette directory is never silent. Pass with `--gate` to
    enforce the regression rule (`GoldenFailure` → exit 1).
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
from evals.recording import CassetteMissing, recorded_trace_producer
from evals.results.schema import SuiteResult
from evals.runner import Producer, run_case
from evals.spreadsheet_bench import runner as sb_runner
from shotcut.spreadsheet.workbook import Workbook


def _empty_producer() -> Producer:
    """Producer used by the SpreadsheetBench CI path when no cassettes
    exist: returns a blank workbook so every case fails scoring loudly.
    Production weekly cron replaces this with a real orchestrator
    producer against live Anthropic."""
    async def producer(_prompt: str) -> Workbook:
        from openpyxl import Workbook as OpenpyxlWorkbook

        return Workbook(OpenpyxlWorkbook())

    return producer


async def _run_golden() -> SuiteResult:
    """Run each golden case through its committed cassette.

    A missing cassette surfaces as a verdict=`errored` row so the gate
    fails the PR with a message pointing at the drift. This keeps the
    golden set / cassette directory / baseline.json in lockstep via
    the CI gate rather than trusting authors to remember the 3-file
    commit."""
    results = []
    for case in GOLDEN_CASES:
        try:
            producer = recorded_trace_producer(case.case_id)
        except CassetteMissing as exc:
            # Emit a synthetic errored row so the report carries the
            # failure reason; regression.check_golden will treat it as
            # a regression against baseline's `passed` verdict.
            from evals.results.schema import CaseResult

            results.append(
                CaseResult(
                    case_id=case.case_id,
                    suite="golden",
                    verdict="errored",
                    duration_seconds=0.0,
                    langfuse_trace_id="(cassette-missing)",
                    message=str(exc),
                )
            )
            continue
        results.append(
            await run_case(case, suite="golden", producer=producer)
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
        producer=_empty_producer(),
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
