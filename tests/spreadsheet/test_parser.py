"""Stage 2 acceptance tests for workbook ingestion.

Four criteria from BUILD_PLAN.md + the formula-preservation criterion
added during Stage 2 planning:

1. Parse → serialize → parse: structural equality on cells, formulas,
   named ranges, conditional formatting.
2. Reject files >50MB with HTTP 413.
3. ClamAV scan stub in place (validates the plumbing, not the scanner).
4. Parse → evaluate → serialize → parse: formulas remain formulas, and
   evaluated values match on second parse.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shotcut.api.routes import router
from shotcut.db.session import get_db
from shotcut.security.scan import ScanResult, scan_bytes
from shotcut.spreadsheet.parser import parse
from shotcut.spreadsheet.workbook import Workbook


# ---------------------------------------------------------------------------
# Criterion 1: Parse → serialize → parse structural equality
# ---------------------------------------------------------------------------


def _sheet_cells(wb: Workbook) -> dict[str, dict[str, Any]]:
    """Flatten {sheet_name -> {ref -> value}} for before/after comparison."""
    out: dict[str, dict[str, Any]] = {}
    summary = wb.summary(max_cells_per_sheet=10_000)
    for sheet in summary["sheets"]:
        out[sheet["name"]] = {c["ref"]: c["value"] for c in sheet["cells"]}
    return out


def test_parse_serialize_parse_roundtrip(feature_rich_xlsx: Path, tmp_path: Path) -> None:
    """Structural equality on cells, formulas, named ranges, CF."""
    first = parse(feature_rich_xlsx)
    first_cells = _sheet_cells(first.workbook)
    first_named = {n.name: n.value for n in first.metadata.named_ranges}
    first_cf = {(c.sheet, c.range): sorted(c.rule_types) for c in first.metadata.conditional_formats}
    first_merges = sorted((m.sheet, m.range) for m in first.metadata.merged_ranges)

    resaved = tmp_path / "resaved.xlsx"
    first.workbook.save(resaved)

    second = parse(resaved)
    second_cells = _sheet_cells(second.workbook)
    second_named = {n.name: n.value for n in second.metadata.named_ranges}
    second_cf = {(c.sheet, c.range): sorted(c.rule_types) for c in second.metadata.conditional_formats}
    second_merges = sorted((m.sheet, m.range) for m in second.metadata.merged_ranges)

    assert first_cells == second_cells, "cell values/formulas lost on round-trip"
    assert first_named == second_named, "named ranges lost on round-trip"
    assert first_cf == second_cf, "conditional formatting lost on round-trip"
    assert first_merges == second_merges, "merged ranges lost on round-trip"


def test_hidden_sheet_flag_preserved(feature_rich_xlsx: Path, tmp_path: Path) -> None:
    first = parse(feature_rich_xlsx)
    resaved = tmp_path / "resaved.xlsx"
    first.workbook.save(resaved)
    second = parse(resaved)
    state = {s.name: s.sheet_state for s in second.metadata.sheets}
    assert state["Model"] == "hidden"
    assert state["Inputs"] == "visible"


def test_sheet_protection_flag_preserved(feature_rich_xlsx: Path, tmp_path: Path) -> None:
    first = parse(feature_rich_xlsx)
    resaved = tmp_path / "resaved.xlsx"
    first.workbook.save(resaved)
    second = parse(resaved)
    protected = {s.name: s.protected for s in second.metadata.sheets}
    assert protected["Summary"] is True
    assert protected["Inputs"] is False


def test_data_validation_preserved(feature_rich_xlsx: Path, tmp_path: Path) -> None:
    first = parse(feature_rich_xlsx)
    resaved = tmp_path / "resaved.xlsx"
    first.workbook.save(resaved)
    second = parse(resaved)
    # We added exactly one DV on Inputs!B2, type=decimal
    dvs = [dv for dv in second.metadata.data_validations if dv.sheet == "Inputs"]
    assert len(dvs) == 1
    assert dvs[0].type == "decimal"
    assert "B2" in " ".join(dvs[0].ranges)


# ---------------------------------------------------------------------------
# Criterion 2: Reject files >50MB with HTTP 413
# ---------------------------------------------------------------------------


def _client_without_db() -> TestClient:
    """TestClient whose `get_db` dependency returns a harmless stub.

    The upload endpoint checks size + scan before touching the DB, so the
    stub is never awaited for the 413 path — we just need to satisfy
    FastAPI's dependency resolution.
    """
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)

    async def fake_db() -> Any:
        yield None

    app.dependency_overrides[get_db] = fake_db
    return TestClient(app)


def test_upload_rejects_oversize() -> None:
    """Files larger than the configured max return 413."""
    client = _client_without_db()
    # 51 MB of zero bytes — well over the 50 MB default cap.
    big = b"\x00" * (51 * 1024 * 1024)
    response = client.post(
        f"/sessions/{'0' * 8}-0000-0000-0000-000000000000/upload",
        files={"upload": ("big.xlsx", big, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert response.status_code == 413
    assert "limit" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Criterion 3: ClamAV scan stub in place
# ---------------------------------------------------------------------------


def test_scan_stub_returns_clean_on_any_input() -> None:
    """Stub always returns clean; Stage 8 swaps to real scanner."""
    result = scan_bytes(b"totally fine bytes")
    assert isinstance(result, ScanResult)
    assert result.clean is True
    assert result.threat is None


def test_upload_rejects_when_scanner_flags_threat(
    monkeypatch: pytest.MonkeyPatch, simple_xlsx: Path
) -> None:
    """If the scan returns not-clean, the endpoint returns 400."""
    from shotcut.api import routes

    def fake_scan(_data: bytes) -> ScanResult:
        return ScanResult(clean=False, threat="EICAR-Test-Signature")

    monkeypatch.setattr(routes, "scan_bytes", fake_scan)

    client = _client_without_db()
    contents = simple_xlsx.read_bytes()
    response = client.post(
        f"/sessions/{'0' * 8}-0000-0000-0000-000000000000/upload",
        files={"upload": ("simple.xlsx", contents, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert response.status_code == 400
    assert "EICAR" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Criterion 4: Parse → evaluate → serialize → parse preserves formulas
# ---------------------------------------------------------------------------


def test_formulas_survive_evaluate_then_save(
    feature_rich_xlsx: Path, tmp_path: Path
) -> None:
    """Guards against the data_only=True silent-strip failure mode.

    Scenario: load the workbook, run the Stage 1 engine to evaluate a
    cell, save to disk, reload. The saved file must still contain
    formulas (not their cached values), and a second evaluation must
    produce the same result.
    """
    first = parse(feature_rich_xlsx)
    wb = first.workbook

    # Evaluate a formula cell on first parse. Model!D5 = SUM of profits.
    first_value = wb.evaluate_cell("Model", "D5")
    assert isinstance(first_value, (int, float))

    # Round-trip through disk.
    resaved = tmp_path / "resaved.xlsx"
    wb.save(resaved)

    second = parse(resaved)

    # (a) Formulas still present on disk (not replaced with values).
    #     openpyxl stores formula cells with `.value` starting with '='.
    model_ws = second.workbook.raw["Model"]
    assert isinstance(model_ws["D5"].value, str)
    assert model_ws["D5"].value.startswith("="), (
        f"Model!D5 should still be a formula after save; got {model_ws['D5'].value!r}"
    )
    # Cross-sheet formula survives too.
    assert isinstance(model_ws["B2"].value, str) and model_ws["B2"].value.startswith("=")

    # (b) Re-evaluation on the reloaded workbook matches the original.
    second_value = second.workbook.evaluate_cell("Model", "D5")
    assert second_value == first_value


# ---------------------------------------------------------------------------
# Miscellany
# ---------------------------------------------------------------------------


def test_from_xlsx_preserves_formulas(feature_rich_xlsx: Path) -> None:
    """Workbook.from_xlsx (simple constructor) must not use data_only."""
    wb = Workbook.from_xlsx(feature_rich_xlsx)
    model_ws = wb.raw["Model"]
    assert isinstance(model_ws["D5"].value, str)
    assert model_ws["D5"].value.startswith("=")


def test_warnings_surface_vba_and_external_links(feature_rich_xlsx: Path) -> None:
    """The clean fixture has neither VBA nor external links; no spurious warnings."""
    parsed = parse(feature_rich_xlsx)
    assert parsed.metadata.has_vba is False
    assert parsed.metadata.external_links == []
    # Warnings should be empty for a clean fixture.
    assert parsed.metadata.warnings == []
