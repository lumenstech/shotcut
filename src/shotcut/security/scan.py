"""Upload virus-scanning — pluggable backends.

Three implementations share the `VirusScanner` protocol:

- `StubScanner` — always clean. Keeps the Stage 2 default contract for
  dev environments without a scanner configured.
- `PatternScanner` — matches a caller-configured list of byte patterns,
  including the industry-standard EICAR antivirus test signature. Used
  in CI/tests where running clamd is overkill.
- `ClamdScanner` — talks to a real `clamd` daemon via its TCP socket
  (`INSTREAM` protocol). Production.

The module-level `scan_bytes()` preserves the Stage 2 call site; it
now delegates to whichever scanner `settings.scan_backend` selects.
"""
from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass
from functools import lru_cache

from shotcut.config import settings


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    threat: str | None = None


# Industry-standard antivirus test signature. Any real scanner must flag
# this as "Eicar-Test-Signature". Hardcoded here so tests can write
# bytes that look like real malware without being real malware.
EICAR_SIGNATURE = (
    br"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)


class VirusScanner(abc.ABC):
    @abc.abstractmethod
    async def scan(self, data: bytes) -> ScanResult:
        ...


class StubScanner(VirusScanner):
    """Legacy default. Always reports clean."""

    async def scan(self, _data: bytes) -> ScanResult:
        return ScanResult(clean=True)


class PatternScanner(VirusScanner):
    """Scans for a fixed set of byte patterns.

    Default patterns include the EICAR signature so the Stage 8
    acceptance test ("upload of EICAR test file → rejected by scanner")
    passes without a clamd dependency.
    """

    def __init__(
        self, patterns: dict[str, bytes] | None = None
    ) -> None:
        self._patterns: dict[str, bytes] = patterns or {
            "EICAR-Test-Signature": EICAR_SIGNATURE,
        }

    async def scan(self, data: bytes) -> ScanResult:
        for name, pattern in self._patterns.items():
            if pattern in data:
                return ScanResult(clean=False, threat=name)
        return ScanResult(clean=True)


class ClamdScanner(VirusScanner):
    """Delegates to a running `clamd` daemon via INSTREAM.

    Requires the `clamd` Python client. Import is deferred so dev
    environments that pick `scan_backend=stub` don't need the package
    installed.
    """

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port

    async def scan(self, data: bytes) -> ScanResult:
        result = await asyncio.to_thread(self._scan_sync, data)
        return result

    def _scan_sync(self, data: bytes) -> ScanResult:
        try:
            import clamd  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "ClamdScanner requires the `clamd` package; install it "
                "or switch SCAN_BACKEND to 'stub' / 'pattern'"
            ) from exc
        client = clamd.ClamdNetworkSocket(host=self._host, port=self._port)
        response = client.instream(data)
        # clamd returns `{"stream": ("OK" | "FOUND", threat_or_None)}`
        status, threat = response.get("stream", ("UNKNOWN", None))
        if status == "FOUND":
            return ScanResult(clean=False, threat=threat)
        if status == "OK":
            return ScanResult(clean=True)
        return ScanResult(clean=False, threat=f"clamd: {status}")


@lru_cache(maxsize=1)
def _scanner() -> VirusScanner:
    """Process-wide scanner resolved from `settings.scan_backend`.

    Cached so the clamd TCP connect happens once. Tests clear this
    cache via `_scanner.cache_clear()` when they swap backends.
    """
    backend = settings.scan_backend.lower()
    if backend == "stub":
        return StubScanner()
    if backend == "pattern":
        return PatternScanner()
    if backend == "clamd":
        return ClamdScanner(host=settings.clamd_host, port=settings.clamd_port)
    raise ValueError(f"unknown SCAN_BACKEND: {settings.scan_backend!r}")


async def scan_bytes(data: bytes) -> ScanResult:
    """Scan an uploaded blob. Preserves the Stage 2 call-site contract.

    Delegates to the process-wide scanner. Callers don't need to know
    which backend is active; tests flip it via the `SCAN_BACKEND`
    setting and clear `_scanner.cache_clear()` between cases.
    """
    scanner = _scanner()
    return await scanner.scan(data)
