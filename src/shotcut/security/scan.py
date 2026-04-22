"""Upload virus-scanning.

Stage 2 stub: `scan_bytes()` always returns `ScanResult(clean=True)`. The
call site (upload endpoint) treats a non-clean result as a 400 rejection,
so the plumbing is in place. Stage 8 wires this to a real ClamAV daemon
(likely via `clamd`'s Unix socket).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    threat: str | None = None


def scan_bytes(_data: bytes) -> ScanResult:
    """Scan an uploaded blob. Stub returns clean unconditionally.

    Kept intentionally simple: no I/O, no flags. Stage 8 replaces the body
    with a real scan; callers should not change.
    """
    return ScanResult(clean=True)
