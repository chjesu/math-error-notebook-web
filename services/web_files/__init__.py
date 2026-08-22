"""Safe file intake primitives for local and object-storage adapters."""

from .intake import FileCandidate, FileIntake

__all__ = ["FileCandidate", "FileIntake"]
