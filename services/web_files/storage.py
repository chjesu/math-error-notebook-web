"""Storage contracts for user-owned file objects."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Protocol
import hashlib
import re
import secrets


class StorageAdapter(Protocol):
    """Persist opaque bytes under application-generated object keys."""

    def save_bytes(self, object_key: str, content: bytes, content_type: str) -> bool:
        """Return true only when this call created the stored object."""
        ...

    def read_bytes(self, object_key: str) -> bytes: ...

    def delete_path(self, object_key: str) -> None: ...


class LocalFsStorageAdapter:
    """Fail-closed local filesystem implementation of ``StorageAdapter``."""

    _WINDOWS_RESERVED_NAMES = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @staticmethod
    def _key(value: str) -> PurePosixPath:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 1024
            or "\\" in value
            or value.startswith("/")
        ):
            raise ValueError("unsafe object key")
        parts = value.split("/")
        if any(
            part in {"", ".", ".."}
            or part.endswith((".", " "))
            or not re.fullmatch(r"[A-Za-z0-9._-]+", part)
            or part.split(".", 1)[0].upper() in LocalFsStorageAdapter._WINDOWS_RESERVED_NAMES
            for part in parts
        ):
            raise ValueError("unsafe object key")
        key = PurePosixPath(value)
        if key.is_absolute():
            raise ValueError("unsafe object key")
        return key

    def _target(self, object_key: str) -> Path:
        key = self._key(object_key)
        target = (self.root / Path(*key.parts)).resolve()
        if self.root not in target.parents:
            raise ValueError("unsafe object key")
        return target

    def save_bytes(self, object_key: str, content: bytes, content_type: str) -> bool:
        if not isinstance(content, bytes) or not isinstance(content_type, str) or not content_type:
            raise ValueError("invalid stored object")
        target = self._target(object_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as handle:
                handle.write(content)
            return True
        except FileExistsError:
            if not target.is_file():
                raise RuntimeError("storage object collision")
            existing = target.read_bytes()
            if not secrets.compare_digest(hashlib.sha256(existing).digest(), hashlib.sha256(content).digest()):
                raise RuntimeError("storage object collision")
            return False

    def read_bytes(self, object_key: str) -> bytes:
        return self.resolve(object_key).read_bytes()

    def delete_path(self, object_key: str) -> None:
        target = self._target(object_key)
        if target.exists() and not target.is_file():
            raise RuntimeError("storage object is not a file")
        target.unlink(missing_ok=True)

    def resolve(self, object_key: str) -> Path:
        target = self._target(object_key)
        if not target.is_file():
            raise LookupError("file not found")
        return target
