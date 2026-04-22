"""Labeled cell-level diff between two workbook states.

Used by the orchestrator to summarize what a turn changed, and by Stage 5
replay to verify a reconstructed state matches its recorded audit trail.

Labels, per the Stage 3 amendments:

- `AGENT_ADDED`           — cell was empty at session start, now has agent content
- `AGENT_OVERWROTE_AGENT` — cell was agent-sourced, got a new agent value
- `AGENT_OVERWROTE_USER`  — user-sourced cell changed via a force-override action
- `USER_MODIFIED`         — user-sourced cell changed *without* a matching
                            force-override action; defensive assertion.
                            If this ever fires, something has bypassed the
                            orchestrator (tenant leak, concurrent write,
                            bug in the state machine).

`USER_MODIFIED` is distinguishable from `AGENT_OVERWROTE_USER` only with
the caller-supplied `force_override_cells` set. The diff engine takes
that set as input rather than inferring from audit rows — the audit
cross-reference belongs in the orchestrator.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shotcut.spreadsheet.occupancy import OccupancyMap
    from shotcut.spreadsheet.workbook import Workbook


class DiffLabel(str, enum.Enum):
    AGENT_ADDED = "agent_added"
    AGENT_OVERWROTE_AGENT = "agent_overwrote_agent"
    AGENT_OVERWROTE_USER = "agent_overwrote_user"
    USER_MODIFIED = "user_modified"


@dataclass(frozen=True)
class CellDiff:
    sheet: str
    ref: str
    before: object
    after: object
    label: DiffLabel


class UserModificationError(AssertionError):
    """Defensive: a user-sourced cell changed outside the force-override path.

    The orchestrator is the only component that mutates cells within a
    session. If this fires, something — tenant isolation, concurrent write,
    a bug — has bypassed the state machine. Don't catch this; fail loud.
    """


def diff(
    before: Workbook,
    after: Workbook,
    occupancy: OccupancyMap,
    force_override_cells: set[tuple[str, str]] | None = None,
) -> list[CellDiff]:
    """Enumerate cells that changed between `before` and `after`, with labels.

    `force_override_cells` is the set of (sheet, ref) that applied with
    `force_override=True` during the window covered by the diff. Pass an
    empty set for the common case (no forced overrides).
    """
    forced = force_override_cells or set()

    before_summary = {s["name"]: s for s in before.summary(max_cells_per_sheet=10_000)["sheets"]}
    after_summary = {s["name"]: s for s in after.summary(max_cells_per_sheet=10_000)["sheets"]}

    out: list[CellDiff] = []
    for name, after_sheet in after_summary.items():
        before_cells = {
            cell["ref"]: cell["value"]
            for cell in before_summary.get(name, {}).get("cells", [])
        }
        for cell in after_sheet["cells"]:
            ref = cell["ref"]
            before_value = before_cells.get(ref)
            after_value = cell["value"]
            if before_value == after_value:
                continue

            label = _label_change(
                sheet=name,
                ref=ref,
                before_value=before_value,
                occupancy=occupancy,
                forced=forced,
            )
            out.append(
                CellDiff(
                    sheet=name,
                    ref=ref,
                    before=before_value,
                    after=after_value,
                    label=label,
                )
            )

    return out


def _label_change(
    *,
    sheet: str,
    ref: str,
    before_value: object,
    occupancy: OccupancyMap,
    forced: set[tuple[str, str]],
) -> DiffLabel:
    is_user_cell = occupancy.is_user_occupied(sheet, ref)

    if is_user_cell:
        if (sheet, ref) in forced:
            return DiffLabel.AGENT_OVERWROTE_USER
        # Defensive: a user-sourced cell changed without going through a
        # force-override action. Something has escaped the state machine.
        raise UserModificationError(
            f"user-sourced cell {sheet}!{ref} changed outside of force-override path"
        )

    if before_value is None:
        return DiffLabel.AGENT_ADDED
    return DiffLabel.AGENT_OVERWROTE_AGENT
