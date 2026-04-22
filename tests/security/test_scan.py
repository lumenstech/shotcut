"""Stage 8 acceptance: upload of EICAR test file → rejected by scanner.

Exercises the `PatternScanner` backend, which detects the industry-
standard EICAR antivirus test signature without requiring a real
clamd daemon in CI.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shotcut.api.routes import router
from shotcut.auth.middleware import get_current_user
from shotcut.auth.tenancy import UserContext
from shotcut.security import scan as scan_mod
from shotcut.security.scan import (
    EICAR_SIGNATURE,
    PatternScanner,
    ScanResult,
    StubScanner,
)


async def test_stub_scanner_always_clean() -> None:
    result = await StubScanner().scan(b"anything")
    assert result == ScanResult(clean=True, threat=None)


async def test_pattern_scanner_detects_eicar() -> None:
    result = await PatternScanner().scan(EICAR_SIGNATURE)
    assert result.clean is False
    assert result.threat == "EICAR-Test-Signature"


async def test_pattern_scanner_clean_for_benign_bytes() -> None:
    result = await PatternScanner().scan(b"just some regular file bytes")
    assert result.clean is True


async def test_pattern_scanner_detects_eicar_embedded_in_larger_blob() -> None:
    """Upload sanitization: EICAR embedded in a larger payload still trips
    the match — ensures we match the signature as a substring, not full
    string equality."""
    padded = b"leading bytes\x00\x00" + EICAR_SIGNATURE + b"\x00\x00trailing bytes"
    result = await PatternScanner().scan(padded)
    assert result.clean is False


async def test_pattern_scanner_accepts_custom_patterns() -> None:
    custom = PatternScanner(patterns={"custom-sig": b"BEEP-BOOP-MALWARE"})
    # Default EICAR pattern is disabled when caller supplies a dict.
    assert (await custom.scan(EICAR_SIGNATURE)).clean is True
    assert (await custom.scan(b"hello BEEP-BOOP-MALWARE world")).threat == "custom-sig"


# ---------------------------------------------------------------------------
# End-to-end: EICAR upload through the HTTP endpoint is rejected
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_with_pattern_scanner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """FastAPI app wired to a PatternScanner + an ephemeral SQLite DB.

    The scan check runs before the DB lookup, so a nonexistent session
    means a clean upload returns 404 (not a DB error); an EICAR upload
    returns 400 from the scan path without touching the DB.
    """
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from shotcut import storage as storage_mod
    from shotcut.config import settings
    from shotcut.db.models import Base
    from shotcut.db.session import get_db

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "scan_backend", "pattern")
    storage_mod.get_storage.cache_clear()
    scan_mod._scanner.cache_clear()

    db_path = tmp_path / "scan.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    app = FastAPI()
    app.include_router(router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: UserContext.anonymous()

    try:
        yield app
    finally:
        await engine.dispose()
        scan_mod._scanner.cache_clear()
        storage_mod.get_storage.cache_clear()


def test_eicar_upload_is_rejected(app_with_pattern_scanner: FastAPI) -> None:
    """The Stage 8 acceptance flow: uploading the EICAR test signature
    returns 400 from the scan path."""
    client = TestClient(app_with_pattern_scanner)

    import uuid

    response = client.post(
        f"/sessions/{uuid.uuid4()}/upload",
        files={
            "upload": (
                "eicar.xlsx",
                EICAR_SIGNATURE,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 400
    assert "EICAR" in response.json()["detail"]


def test_clean_upload_passes_scanner(app_with_pattern_scanner: FastAPI) -> None:
    """A benign tiny payload passes the scanner (it may 404 downstream
    because the session doesn't exist — we only care that the scanner
    didn't reject it)."""
    import uuid

    client = TestClient(app_with_pattern_scanner)
    response = client.post(
        f"/sessions/{uuid.uuid4()}/upload",
        files={
            "upload": (
                "clean.xlsx",
                b"non-malicious bytes",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    # Not 400 — the scanner didn't fire. Could be 404 (no session) or
    # 500 (openpyxl can't parse our fake bytes) — both fine here; we're
    # only asserting the scan path didn't veto.
    assert response.status_code != 400
