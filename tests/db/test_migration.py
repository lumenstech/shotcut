"""Alembic migration smoke tests.

Covers the Stage 3 consolidated migration:

- Revision chain has one head, no down-revision (initial migration)
- `alembic upgrade head` produces the expected tables/columns/indexes
- `alembic downgrade base` tears down cleanly
- Round-trip (upgrade → downgrade → upgrade) leaves no artifacts

Runs against an ephemeral SQLite DB so CI doesn't need Postgres. Native
ENUM types are pg-specific; `sa.Enum` falls back to VARCHAR + CHECK on
SQLite automatically, so the same migration script works in both.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _make_config(db_url: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


# ---------------------------------------------------------------------------
# Structural tests (no DB)
# ---------------------------------------------------------------------------


def test_revision_chain_is_linear() -> None:
    """Stage 3 (0001) → Stage 5 erratum (0002) → Stage 5 branches (0003)
    → Stage 6 orchestrator_states (0004) → Stage 7 financial_facts (0005)
    → Stage 8 RLS policies (0006). Single linear chain, one head."""
    cfg = _make_config("sqlite://")
    scripts = ScriptDirectory.from_config(cfg)
    revisions = {r.revision: r for r in scripts.walk_revisions()}
    assert set(revisions) == {"0001", "0002", "0003", "0004", "0005", "0006"}
    assert revisions["0001"].down_revision is None
    assert revisions["0002"].down_revision == "0001"
    assert revisions["0003"].down_revision == "0002"
    assert revisions["0004"].down_revision == "0003"
    assert revisions["0005"].down_revision == "0004"
    assert revisions["0006"].down_revision == "0005"
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert heads[0] == "0006"


# ---------------------------------------------------------------------------
# Upgrade / downgrade round-trip against SQLite
# ---------------------------------------------------------------------------


def _sync_sqlite_url(tmp_path: Path) -> str:
    """Sync SQLAlchemy URL — Alembic runs migrations synchronously. Our
    async async engine used by the app runs against `sqlite+aiosqlite`,
    but for migration invocation we use the stdlib sqlite3 driver."""
    return f"sqlite:///{tmp_path / 'migration_test.db'}"


def test_upgrade_creates_expected_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`alembic upgrade head` on a fresh DB creates sessions + actions."""
    # The env.py reads settings.database_url; override for this test.
    from shotcut.config import settings

    url = _sync_sqlite_url(tmp_path)
    monkeypatch.setattr(settings, "database_url", url)

    cfg = _make_config(url)
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) >= {"sessions", "actions", "alembic_version"}

    action_cols = {c["name"] for c in inspector.get_columns("actions")}
    required = {
        "id", "session_id", "sequence", "agent", "action_type",
        "sheet", "target_range", "previous_value", "new_value", "reasoning",
        "created_at", "status", "approval_required_reason", "force_override",
        "parent_action_id", "tenant_id", "user_sub",
        # Stage 5 erratum.
        "client_action_id",
    }
    missing = required - action_cols
    assert not missing, f"actions table missing columns: {missing}"

    session_cols = {c["name"] for c in inspector.get_columns("sessions")}
    assert "original_workbook_path" in session_cols
    assert "tenant_id" in session_cols

    # Stage-5 / Stage-8 indexes present from day one.
    action_indexes = {ix["name"] for ix in inspector.get_indexes("actions")}
    assert "ix_actions_parent_action_id" in action_indexes
    assert "ix_actions_status" in action_indexes
    assert "ix_actions_tenant_id" in action_indexes
    assert "ix_actions_client_action_id" in action_indexes

    session_indexes = {ix["name"] for ix in inspector.get_indexes("sessions")}
    assert "ix_sessions_original_workbook_path" in session_indexes
    assert "ix_sessions_tenant_id" in session_indexes

    engine.dispose()


def test_downgrade_then_upgrade_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """upgrade head → downgrade base → upgrade head works cleanly."""
    from shotcut.config import settings

    url = _sync_sqlite_url(tmp_path)
    monkeypatch.setattr(settings, "database_url", url)

    cfg = _make_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = create_engine(url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    # Only the alembic version table should remain after downgrade base;
    # `sessions` and `actions` are dropped.
    assert "sessions" not in tables
    assert "actions" not in tables
    engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) >= {"sessions", "actions"}
    engine.dispose()


def test_stage5_erratum_backfills_existing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 5 erratum (0002) runs cleanly on a Stage-3/4 populated DB.

    Seeds Stage 3 (0001) state with action rows lacking `client_action_id`,
    then applies 0002 and asserts every row got a unique non-null UUID.
    """
    from shotcut.config import settings
    from sqlalchemy import text

    url = _sync_sqlite_url(tmp_path)
    monkeypatch.setattr(settings, "database_url", url)

    cfg = _make_config(url)
    # Stop at 0001 so the table is in the pre-erratum shape.
    command.upgrade(cfg, "0001")

    engine = create_engine(url)
    session_id = "00000000-0000-0000-0000-000000000001"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sessions (id, workbook_path) VALUES (:id, :wp)"
            ),
            {"id": session_id, "wp": "/tmp/wb.xlsx"},
        )
        for i in range(5):
            conn.execute(
                text(
                    "INSERT INTO actions "
                    "(id, session_id, sequence, agent, action_type, status, force_override) "
                    "VALUES (:id, :sid, :seq, 'executor', 'write_value', 'applied', 0)"
                ),
                {
                    "id": f"00000000-0000-0000-0000-00000000000{i + 2}",
                    "sid": session_id,
                    "seq": i + 1,
                },
            )
    engine.dispose()

    # Apply the erratum migration on top of the populated DB.
    command.upgrade(cfg, "0002")

    engine = create_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, client_action_id FROM actions ORDER BY sequence")
        ).fetchall()
    engine.dispose()

    assert len(rows) == 5
    ids = {r[1] for r in rows}
    assert len(ids) == 5, "backfilled client_action_ids must be unique per row"
    assert all(r[1] is not None for r in rows), "every row must have a non-null backfill"
