"""SpreadsheetBench runner shell.

The full dataset (912 cases) is not bundled in this repo — too large,
too expensive to run on every push, and not something CI should fetch.
Production points this runner at a shared storage location on a
weekly cron.

The shell here:
  - Takes a `dataset_root: Path` and a subset filter (count + ids).
  - Iterates cases on disk, building `EvalCase` objects.
  - Delegates the actual scoring to `evals.runner.run_suite`.

Tests pass a tiny fixture dataset in `tmp_path` that covers every
code path without hitting Anthropic or the real SpreadsheetBench files.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from evals.results.schema import CaseResult, SuiteResult
from evals.runner import EvalCase, Producer, run_suite
from shotcut.spreadsheet.workbook import Workbook


@dataclass(frozen=True)
class DatasetFilter:
    """Subset of SpreadsheetBench to run.

    CI passes a small count; cron passes None (everything).
    """

    max_cases: int | None = None
    only_case_ids: frozenset[str] | None = None

    def keep(self, case_id: str, index: int) -> bool:
        if self.only_case_ids is not None and case_id not in self.only_case_ids:
            return False
        if self.max_cases is not None and index >= self.max_cases:
            return False
        return True


async def run(
    *,
    dataset_root: Path,
    producer: Producer,
    filter: DatasetFilter = DatasetFilter(),
) -> SuiteResult:
    """Run the SpreadsheetBench cases under `dataset_root`.

    Expects the dataset layout:
      dataset_root/
        manifest.json     # [{"case_id": "...", "prompt": "...", "expected": "relpath.xlsx"}, ...]
        cases/<case_id>/expected.xlsx
    """
    manifest_path = dataset_root / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"SpreadsheetBench manifest not found: {manifest_path}"
        )
    manifest = json.loads(manifest_path.read_text())
    cases: list[EvalCase] = []
    for idx, entry in enumerate(manifest):
        case_id = entry["case_id"]
        if not filter.keep(case_id, idx):
            continue
        expected_path = dataset_root / entry["expected"]
        cases.append(
            EvalCase(
                case_id=case_id,
                prompt=entry["prompt"],
                expected=Workbook.from_xlsx(expected_path),
            )
        )

    results: list[CaseResult] = await run_suite(
        cases, suite="spreadsheetbench", producer=producer
    )
    return SuiteResult.from_cases(suite="spreadsheetbench", cases=results)
