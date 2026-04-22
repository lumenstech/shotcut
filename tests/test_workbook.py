from pathlib import Path

from shotcut.spreadsheet.actions import (
    AddSheet,
    FormatCell,
    WriteFormula,
    WriteValue,
)
from shotcut.spreadsheet.workbook import Workbook


def test_apply_and_roundtrip(tmp_path: Path):
    wb = Workbook.blank()

    wb.apply(AddSheet(sheet="Model"))
    wb.apply(WriteValue(sheet="Model", target="A1", value="Revenue"))
    wb.apply(WriteValue(sheet="Model", target="B1", value=1000))
    wb.apply(WriteValue(sheet="Model", target="B2", value=1100))
    wb.apply(WriteFormula(sheet="Model", target="B3", formula="=B2/B1-1"))
    wb.apply(FormatCell(sheet="Model", target="B3", number_format="0.00%"))

    path = tmp_path / "out.xlsx"
    wb.save(path)

    reloaded = Workbook.load(path)
    summary = reloaded.summary()
    model_sheet = next(s for s in summary["sheets"] if s["name"] == "Model")
    values = {c["ref"]: c["value"] for c in model_sheet["cells"]}
    assert values["A1"] == "Revenue"
    assert values["B3"] == "=B2/B1-1"


def test_summary_truncates():
    wb = Workbook.blank()
    for i in range(1, 60):
        wb.apply(WriteValue(sheet="Sheet", target=f"A{i}", value=i))

    summary = wb.summary(max_cells_per_sheet=10)
    sheet = summary["sheets"][0]
    assert sheet["truncated"]
    assert len(sheet["cells"]) == 10
