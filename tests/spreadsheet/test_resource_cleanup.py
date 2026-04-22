"""Regression: repeated workbook loads must not leak file handles.

Stage 4's verifier loads workbooks far more often than Stages 1–3 did
(once per evaluation pass across many cells). A leaked ZipFile handle
per evaluation — via openpyxl's `vba_archive` ZipFile whose `__del__`
seeks on a freed BytesIO — would surface as intermittent
`PytestUnraisableExceptionWarning` entries and eventually as flaky CI.

These tests gate the fix: load and discard many workbooks, promote
pytest's unraisable-exception warning to an error, and require zero
escapes.
"""
from __future__ import annotations

import gc
from pathlib import Path

import pytest
from openpyxl import Workbook as OpenpyxlWorkbook

from shotcut.spreadsheet.parser import parse
from shotcut.spreadsheet.workbook import Workbook


def _build_simple(path: Path) -> None:
    wb = OpenpyxlWorkbook()
    wb.active.title = "Sheet1"
    wb.active["A1"] = 1
    wb.save(path)


@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
def test_parse_100x_no_unraisable(tmp_path: Path) -> None:
    """Parse + discard 100 workbooks; no ZipFile __del__ exception escapes."""
    path = tmp_path / "s.xlsx"
    _build_simple(path)

    for _ in range(100):
        parsed = parse(path)
        del parsed
    gc.collect()


@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
def test_from_xlsx_100x_no_unraisable(tmp_path: Path) -> None:
    """Workbook.from_xlsx() callers outside the parser path are also clean.

    Stage 4's engine re-loads workbooks directly in several places (evaluate
    across cells, clone-then-mutate for pending-approval simulation). This
    path must be leak-free too.
    """
    path = tmp_path / "s.xlsx"
    _build_simple(path)

    for _ in range(100):
        wb = Workbook.from_xlsx(path)
        wb.close()
        del wb
    gc.collect()


@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
def test_context_manager_closes_cleanly(tmp_path: Path) -> None:
    """`with Workbook.from_xlsx(path) as wb:` is the preferred shape for
    short-lived loads; the context manager must cover the same guarantee."""
    path = tmp_path / "s.xlsx"
    _build_simple(path)

    for _ in range(100):
        with Workbook.from_xlsx(path) as wb:
            assert wb.raw.sheetnames
    gc.collect()


def test_close_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "s.xlsx"
    _build_simple(path)

    wb = Workbook.from_xlsx(path)
    wb.close()
    wb.close()  # must not raise
