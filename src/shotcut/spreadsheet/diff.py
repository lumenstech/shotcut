"""Workbook-to-workbook cell diff for audit summary views."""
from __future__ import annotations

from dataclasses import dataclass

from shotcut.spreadsheet.workbook import Workbook


@dataclass
class CellDiff:
    sheet: str
    ref: str
    before: object
    after: object


def diff(before: Workbook, after: Workbook) -> list[CellDiff]:
    out: list[CellDiff] = []
    before_summary = {s["name"]: s for s in before.summary(max_cells_per_sheet=10_000)["sheets"]}
    after_summary = {s["name"]: s for s in after.summary(max_cells_per_sheet=10_000)["sheets"]}

    for name, after_sheet in after_summary.items():
        before_cells = {c["ref"]: c["value"] for c in before_summary.get(name, {}).get("cells", [])}
        for cell in after_sheet["cells"]:
            prev = before_cells.get(cell["ref"])
            if prev != cell["value"]:
                out.append(CellDiff(sheet=name, ref=cell["ref"], before=prev, after=cell["value"]))
    return out
