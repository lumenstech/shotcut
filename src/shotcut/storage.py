"""Session-scoped workbook file storage.

Abstract interface + a local-filesystem implementation. The signature is
designed so a future `S3WorkbookStorage` is a drop-in — one factory change
in `get_storage()` and zero caller changes. See
`docs/decisions/0001-calc-engine.md` for the overall storage model.

Storage layout (local):
    storage_dir/
        workbooks/
            <session_id>/
                original.xlsx    # user-uploaded
                current.xlsx     # agent-written (optional)
                <N>.xlsx         # named checkpoints (optional)
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path

from shotcut.config import settings


class WorkbookStorage(ABC):
    """Storage for workbook files, keyed by (session_id, name)."""

    @abstractmethod
    def put(self, session_id: uuid.UUID, name: str, data: bytes) -> str:
        """Persist `data` under (session_id, name). Returns a stable URI/path."""

    @abstractmethod
    def get(self, session_id: uuid.UUID, name: str) -> bytes:
        """Read bytes back. Raises FileNotFoundError if absent."""

    @abstractmethod
    def exists(self, session_id: uuid.UUID, name: str) -> bool:
        """Whether (session_id, name) has been written."""

    @abstractmethod
    def delete(self, session_id: uuid.UUID, name: str) -> None:
        """Remove (session_id, name) if it exists. No-op otherwise."""

    @abstractmethod
    def local_path(self, session_id: uuid.UUID, name: str) -> Path | None:
        """Local filesystem path, or None for remote-only backends.

        Openpyxl's loader wants a path or file-like object. When the backend
        is S3, callers should `get()` bytes and wrap in BytesIO instead.
        """


class LocalWorkbookStorage(WorkbookStorage):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, session_id: uuid.UUID) -> Path:
        d = self.root / str(session_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, session_id: uuid.UUID, name: str) -> Path:
        return self._dir(session_id) / name

    def put(self, session_id: uuid.UUID, name: str, data: bytes) -> str:
        path = self._path(session_id, name)
        path.write_bytes(data)
        return str(path)

    def get(self, session_id: uuid.UUID, name: str) -> bytes:
        return self._path(session_id, name).read_bytes()

    def exists(self, session_id: uuid.UUID, name: str) -> bool:
        return self._path(session_id, name).exists()

    def delete(self, session_id: uuid.UUID, name: str) -> None:
        self._path(session_id, name).unlink(missing_ok=True)

    def local_path(self, session_id: uuid.UUID, name: str) -> Path | None:
        return self._path(session_id, name)


@lru_cache(maxsize=1)
def get_storage() -> WorkbookStorage:
    """Default storage for the process. Swap the return for S3 when we lift."""
    return LocalWorkbookStorage(settings.storage_dir / "workbooks")
