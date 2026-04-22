"""Ingest user-uploaded `.xlsx` files with full structural fidelity.

`parse(path)` returns a `ParsedWorkbook` pairing the internal `Workbook`
with a `ParseMetadata` describing what the file contains. The workbook
object itself captures cell values, formulas, named ranges, merged
cells, data validation, conditional formatting, hidden sheets, and
sheet protection — everything openpyxl round-trips correctly.

Things we intentionally do NOT try to preserve on a later save:

- **VBA macros.** Detected (`has_vba`) and surfaced as a warning. We do
  not execute, edit, or re-emit them — the engine would need to be an
  .xlsm signer and the security surface is too large for Stage 2.
- **External links.** Detected and surfaced as a warning. Followed up
  in Stage 7 (researcher) when we have a sanctioned data path.
- **Pivot tables.** Noted in metadata (sheet + location). Openpyxl's
  pivot-table support is read-only today; recreating one on save is
  explicit non-goal for the MVP.

The `metadata.warnings` list is surfaced through the upload response so
users know what survived the round-trip and what didn't.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from pydantic import BaseModel

from shotcut.spreadsheet.workbook import Workbook


class NamedRangeMeta(BaseModel):
    name: str
    scope_sheet: str | None  # None = workbook-scoped
    value: str  # attr_text, e.g. "Sheet1!$A$1:$A$10"


class MergedRangeMeta(BaseModel):
    sheet: str
    range: str  # e.g. "A1:B3"


class DataValidationMeta(BaseModel):
    sheet: str
    type: str | None  # "list", "whole", "decimal", "date", ...
    formula1: str | None
    formula2: str | None
    ranges: list[str]


class ConditionalFormatMeta(BaseModel):
    sheet: str
    range: str
    rule_types: list[str]  # e.g. ["cellIs", "expression"]


class SheetMeta(BaseModel):
    name: str
    sheet_state: str  # "visible" | "hidden" | "veryHidden"
    protected: bool
    dimensions: str


class PivotTableMeta(BaseModel):
    sheet: str
    name: str | None


class ParseMetadata(BaseModel):
    sheets: list[SheetMeta]
    named_ranges: list[NamedRangeMeta]
    merged_ranges: list[MergedRangeMeta]
    data_validations: list[DataValidationMeta]
    conditional_formats: list[ConditionalFormatMeta]
    pivot_tables: list[PivotTableMeta]
    has_vba: bool
    external_links: list[str]
    warnings: list[str]


class ParsedWorkbook(BaseModel):
    metadata: ParseMetadata

    # Workbook instances aren't pydantic-serializable; held outside the model.
    model_config = {"arbitrary_types_allowed": True}

    workbook: Any  # typed as Workbook at runtime; arbitrary_types_allowed


def parse(path: Path) -> ParsedWorkbook:
    """Parse an .xlsx file. Returns the Workbook plus structural metadata.

    Loads with `data_only=False` so formulas are preserved as formulas.
    `keep_vba=True` means an .xlsm file's binary VBA archive is retained
    internally by openpyxl for later resave; we don't expose or execute it.
    """
    pyxl = load_workbook(path, data_only=False, keep_vba=True, keep_links=True)
    workbook = Workbook(pyxl)
    metadata = _extract_metadata(pyxl)
    return ParsedWorkbook(metadata=metadata, workbook=workbook)


def _extract_metadata(pyxl: Any) -> ParseMetadata:
    warnings: list[str] = []

    # Sheets
    sheets: list[SheetMeta] = []
    merged_ranges: list[MergedRangeMeta] = []
    data_validations: list[DataValidationMeta] = []
    conditional_formats: list[ConditionalFormatMeta] = []
    pivot_tables: list[PivotTableMeta] = []

    for sheet_name in pyxl.sheetnames:
        ws = pyxl[sheet_name]

        sheets.append(
            SheetMeta(
                name=sheet_name,
                sheet_state=str(ws.sheet_state),
                protected=bool(getattr(ws.protection, "sheet", False)),
                dimensions=str(ws.dimensions),
            )
        )

        for merged in ws.merged_cells.ranges:
            merged_ranges.append(MergedRangeMeta(sheet=sheet_name, range=str(merged)))

        # data_validations exposes .dataValidation list of DataValidation objects
        for dv in getattr(ws.data_validations, "dataValidation", []) or []:
            data_validations.append(
                DataValidationMeta(
                    sheet=sheet_name,
                    type=getattr(dv, "type", None),
                    formula1=getattr(dv, "formula1", None),
                    formula2=getattr(dv, "formula2", None),
                    ranges=[str(r) for r in (dv.sqref.ranges if dv.sqref else [])],
                )
            )

        # conditional_formatting yields (range, [rules]) pairs when iterated
        for cf_range, rules in ws.conditional_formatting._cf_rules.items():
            rule_types = [getattr(r, "type", "unknown") for r in rules]
            conditional_formats.append(
                ConditionalFormatMeta(
                    sheet=sheet_name,
                    range=str(cf_range),
                    rule_types=rule_types,
                )
            )

        # Pivot tables: openpyxl stores them on ws._pivots (private list). We
        # note presence and give up on preserving structure.
        pivot_list = getattr(ws, "_pivots", None) or []
        for pt in pivot_list:
            pivot_tables.append(
                PivotTableMeta(sheet=sheet_name, name=getattr(pt, "name", None))
            )
        if pivot_list:
            warnings.append(
                f"sheet {sheet_name!r} contains {len(pivot_list)} pivot table(s); "
                "structure is noted but not preserved on save."
            )

    # Named ranges
    named_ranges: list[NamedRangeMeta] = []
    for name, defined in pyxl.defined_names.items():
        # localSheetId is an int (sheet index) when sheet-scoped, else None.
        scope_sheet: str | None = None
        local_id = getattr(defined, "localSheetId", None)
        if local_id is not None and 0 <= local_id < len(pyxl.sheetnames):
            scope_sheet = pyxl.sheetnames[local_id]
        named_ranges.append(
            NamedRangeMeta(
                name=name,
                scope_sheet=scope_sheet,
                value=str(defined.attr_text or ""),
            )
        )

    # VBA and external links.
    #
    # openpyxl sets `vba_archive` to a ZipFile for every workbook loaded with
    # `keep_vba=True`, not just .xlsm files — the archive is the workbook's
    # own container. Real VBA content lives at `xl/vbaProject.bin` inside
    # that archive, so check for the entry rather than for the archive
    # object itself.
    has_vba = False
    vba_archive = getattr(pyxl, "vba_archive", None)
    if vba_archive is not None:
        try:
            has_vba = "xl/vbaProject.bin" in vba_archive.namelist()
        except Exception:
            has_vba = False
    if has_vba:
        warnings.append("workbook contains VBA macros; they are preserved in the "
                        "binary but not executed or editable via the API.")

    external_links: list[str] = []
    for link in getattr(pyxl, "_external_links", []) or []:
        # Each link has a .file_link.Target for the URI, if present
        target = getattr(getattr(link, "file_link", None), "Target", None)
        if target:
            external_links.append(str(target))
    if external_links:
        warnings.append(
            f"workbook references {len(external_links)} external link(s); formulas "
            "pointing at them will not resolve in-engine."
        )

    return ParseMetadata(
        sheets=sheets,
        named_ranges=named_ranges,
        merged_ranges=merged_ranges,
        data_validations=data_validations,
        conditional_formats=conditional_formats,
        pivot_tables=pivot_tables,
        has_vba=has_vba,
        external_links=external_links,
        warnings=warnings,
    )
