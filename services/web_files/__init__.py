"""Safe file intake primitives for local and object-storage adapters."""

from .intake import FileCandidate, FileIntake
from .storage import LocalFsStorageAdapter, StorageAdapter

__all__ = ["FileCandidate", "FileIntake", "LocalFsStorageAdapter", "StorageAdapter"]
