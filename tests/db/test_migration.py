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


def test_revision_chain_has_one_head() -> None:
    """Stage 3 is the first migration — exactly one head, no parent."""
    cfg = _make_config("sqlite://")
    scripts = ScriptDirectory.from_config(cfg)
    revisions = list(scripts.walk_revisions())
    assert len(revisions) == 1
    head = revisions[0]
    assert head.revision == "0001"
    assert head.down_revision is None


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
