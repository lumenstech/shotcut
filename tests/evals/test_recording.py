"""Recorded-trace producer tests.

Cassettes are agent-layer replay artifacts. What this suite guards:

- Every committed golden case has a cassette (drift detector).
- Each cassette replays to a workbook that matches the golden
  expected via the scorer.
- CassetteMissing is the error surface — not a silent identity
  passthrough — so CI catches "you added a golden case but forgot
  the cassette" on the next push.
- Cassette schema is validated via Pydantic's discriminated union;
  action-type drift fails cassette load, not mid-replay.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.golden.cases import CASES as GOLDEN_CASES
from evals.recording import (
    CassetteMissing,
    load_cassette,
    recorded_trace_producer,
)
from evals.recording.cassette import cassettes_dir
from evals.scoring import score


# ---------------------------------------------------------------------------
# Every committed golden case has a loadable cassette
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: c.case_id)
def test_every_golden_case_has_a_cassette(case) -> None:
    cassette = load_cassette(case.case_id)
    assert cassette.case_id == case.case_id
    assert cassette.prompt == case.prompt, (
        "cassette prompt drifted from the committed case prompt; either "
        "re-record or update the golden case"
    )


# ---------------------------------------------------------------------------
# Each cassette's replayed workbook matches its expected via the scorer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: c.case_id)
async def test_cassette_replay_passes_scorer(case) -> None:
    producer = recorded_trace_producer(case.case_id)
    actual = await producer(case.prompt)
    report = score(case.expected, actual)
    assert report.passed, (
        f"cassette for {case.case_id} no longer produces the expected "
        f"workbook. Diffs: {[d.model_dump() for d in report.diffs[:3]]}"
    )


# ---------------------------------------------------------------------------
# Missing cassette raises, not silently passes
# ---------------------------------------------------------------------------


def test_missing_cassette_raises_cassette_missing() -> None:
    with pytest.raises(CassetteMissing):
        load_cassette("does-not-exist-case-id-xxx")


def test_producer_factory_raises_at_build_time_on_missing(tmp_path: Path) -> None:
    """`recorded_trace_producer` loads the cassette eagerly so the
    failure happens at CLI startup, not mid-run."""
    with pytest.raises(CassetteMissing):
        recorded_trace_producer("another-missing-case")


# ---------------------------------------------------------------------------
# Cassette schema enforces Action discriminated-union validation
# ---------------------------------------------------------------------------


def test_cassette_with_unknown_action_type_fails_load(tmp_path: Path) -> None:
    """If someone commits a cassette referencing an action type the
    current union doesn't know about, Pydantic rejects it at load.
    This catches "agent ships a new action variant but the cassette
    still uses the old shape" on the very first CI run."""
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "case_id": "bad",
                "prompt": "x",
                "plan": {"summary": "x", "steps": []},
                "executor_outputs": [
                    [
                        {
                            "type": "not_a_real_action",
                            "sheet": "S",
                            "target": "A1",
                        }
                    ]
                ],
                "verifier": {"findings": [], "confidence": 1.0},
            }
        )
    )
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        load_cassette("bad", root=tmp_path)


def test_cassette_with_invalid_action_fields_fails_load(tmp_path: Path) -> None:
    """write_formula without `formula` → Pydantic rejects."""
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "case_id": "bad",
                "prompt": "x",
                "plan": {"summary": "x", "steps": []},
                "executor_outputs": [
                    [
                        {
                            "type": "write_formula",
                            "sheet": "S",
                            "target": "A1",
                            # no `formula` field
                        }
                    ]
                ],
                "verifier": {"findings": [], "confidence": 1.0},
            }
        )
    )
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        load_cassette("bad", root=tmp_path)


# ---------------------------------------------------------------------------
# Cassette directory hygiene
# ---------------------------------------------------------------------------


def test_cassettes_directory_has_one_json_per_golden_case() -> None:
    """No orphan cassettes (recorded case that's been deleted from
    golden/cases.py) and no missing ones."""
    dir_json = {
        p.stem for p in cassettes_dir().glob("*.json")
    }
    case_ids = {c.case_id for c in GOLDEN_CASES}
    orphans = dir_json - case_ids
    missing = case_ids - dir_json
    assert not orphans, (
        f"cassette directory has orphan recordings: {orphans}. "
        "Remove or add them to golden/cases.py."
    )
    assert not missing, (
        f"golden cases missing cassettes: {missing}. "
        "Record them via the recording workflow."
    )


def test_cassette_replay_produces_the_same_workbook_twice() -> None:
    """Replay is deterministic: calling the producer twice yields
    identical workbooks (cell-by-cell)."""
    import asyncio

    async def run() -> None:
        producer = recorded_trace_producer("sum_of_column")
        first = await producer("ignored")
        second = await producer("ignored")
        report = score(first, second)
        assert report.passed, f"non-deterministic replay: {report.diffs}"

    asyncio.run(run())
