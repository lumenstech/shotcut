"""Stage 5 erratum: client_action_id behavior.

Guards the invariants the Stage 5 branching and Stage 6 checkpointing
work will rely on:

- `client_action_id` defaults to a fresh UUID on construction; each
  new Action gets a distinct id.
- Serialize → deserialize preserves the id (the point of the erratum:
  Python `id()` does not survive this round-trip, `client_action_id`
  does).
- audit.record() persists the domain id onto the DB row.
- row_to_action() round-trips the id from DB row back to domain object.
- Attribution via `attribute_to_actions` is stable when the logical
  action is reconstructed from a persisted row.
"""
from __future__ import annotations

import uuid

import pytest_asyncio
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from shotcut.agents.verifier import (
    VerifierFinding,
    VerifierLevel,
    VerifierSeverity,
    attribute_to_actions,
)
from shotcut.db import audit
from shotcut.db.models import Action as ActionRow
from shotcut.db.models import Session as SessionRow
from shotcut.spreadsheet.actions import Action as AgentAction
from shotcut.spreadsheet.actions import (
    AddSheet,
    FormatCell,
    SetColumnWidth,
    WriteFormula,
    WriteValue,
)

_action_adapter: TypeAdapter[AgentAction] = TypeAdapter(AgentAction)


# ---------------------------------------------------------------------------
# Domain model invariants
# ---------------------------------------------------------------------------


def test_every_action_variant_has_client_action_id() -> None:
    """Every variant of the Action discriminated union carries a UUID."""
    actions = [
        WriteValue(sheet="S", target="A1", value=1),
        WriteFormula(sheet="S", target="A1", formula="=1"),
        FormatCell(sheet="S", target="A1", bold=True),
        AddSheet(sheet="S"),
        SetColumnWidth(sheet="S", target="A1", width=10.0),
    ]
    for action in actions:
        assert isinstance(action.client_action_id, uuid.UUID)


def test_default_ids_are_distinct() -> None:
    """Default factory produces fresh UUIDs per construction."""
    a = WriteValue(sheet="S", target="A1", value=1)
    b = WriteValue(sheet="S", target="A1", value=1)
    assert a.client_action_id != b.client_action_id


def test_serialize_deserialize_preserves_id() -> None:
    """The whole point of the erratum: identity survives round-trip."""
    original = WriteValue(sheet="S", target="A1", value=42)
    dumped = original.model_dump(mode="json")
    assert str(original.client_action_id) == dumped["client_action_id"]

    reconstructed = _action_adapter.validate_python(dumped)
    assert reconstructed.client_action_id == original.client_action_id


def test_explicit_client_action_id_is_respected() -> None:
    """Callers can pass an existing id (e.g. when reconstructing from a DB row)."""
    cid = uuid.uuid4()
    action = WriteValue(client_action_id=cid, sheet="S", target="A1", value=1)
    assert action.client_action_id == cid


# ---------------------------------------------------------------------------
# Persistence round-trip via audit.record + row_to_action
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seeded_session(test_db: AsyncSession) -> uuid.UUID:
    session_id = uuid.uuid4()
    test_db.add(
        SessionRow(
            id=session_id,
            workbook_path="/tmp/ignored",
            original_workbook_path=None,
        )
    )
    await test_db.commit()
    return session_id


async def test_audit_record_persists_client_action_id(
    test_db: AsyncSession, seeded_session: uuid.UUID
) -> None:
    action = WriteValue(sheet="S", target="A1", value=1)
    row = await audit.record(
        test_db,
        session_id=seeded_session,
        agent="executor",
        action=action,
        previous_value=None,
    )
    await test_db.commit()

    persisted = await test_db.get(ActionRow, row.id)
    assert persisted is not None
    assert persisted.client_action_id == action.client_action_id


async def test_row_to_action_round_trips_client_action_id(
    test_db: AsyncSession, seeded_session: uuid.UUID
) -> None:
    original = WriteFormula(sheet="S", target="B2", formula="=SUM(A1:A3)")
    row = await audit.record(
        test_db,
        session_id=seeded_session,
        agent="executor",
        action=original,
        previous_value=None,
    )
    await test_db.commit()
    await test_db.refresh(row)

    reconstructed = audit.row_to_action(row)
    assert reconstructed.client_action_id == original.client_action_id


# ---------------------------------------------------------------------------
# Attribution stability across reconstruction
# ---------------------------------------------------------------------------


def test_attribution_stable_across_reconstruction() -> None:
    """Finding attribution works against a reconstructed Action, not just
    the original object. This is the scenario Stage 5 branching and
    Stage 6 checkpoint-resume both create: a logical action is loaded
    from persistence and needs to be matchable against findings.
    """
    original = WriteValue(sheet="Sheet1", target="A1", value=1)
    dumped = original.model_dump(mode="json")
    reconstructed = _action_adapter.validate_python(dumped)

    # Simulate the orchestrator's map: (domain_action, row_id) pairs.
    row_id = uuid.uuid4()
    row_id_map = {reconstructed.client_action_id: row_id}

    finding = VerifierFinding(
        level=VerifierLevel.NUMERICAL,
        severity=VerifierSeverity.CRITICAL,
        sheet="Sheet1",
        cell="A1",
        message="test",
    )
    attribution = attribute_to_actions([finding], [reconstructed], row_id_map)

    # Python id(original) != id(reconstructed); the id()-based attribution
    # would have missed entirely. client_action_id attribution finds it.
    assert row_id in attribution
    assert attribution[row_id] == [finding]


def test_attribution_survives_deep_copy() -> None:
    """Deep copies (pickle-equivalent for Pydantic) preserve attribution."""
    import copy

    action = WriteValue(sheet="Sheet1", target="A1", value=1)
    copied = copy.deepcopy(action)
    assert copied.client_action_id == action.client_action_id

    row_id = uuid.uuid4()
    row_id_map = {action.client_action_id: row_id}
    finding = VerifierFinding(
        level=VerifierLevel.NUMERICAL,
        severity=VerifierSeverity.CRITICAL,
        sheet="Sheet1",
        cell="A1",
        message="test",
    )
    # Passing the deep-copied object; attribution still finds the row.
    assert attribute_to_actions([finding], [copied], row_id_map) == {row_id: [finding]}
