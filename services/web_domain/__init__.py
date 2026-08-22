"""User-scoped domain persistence for the Web edition."""

from .mysql_store import (
    ErrorEntry,
    FileRecord,
    GradeCandidate,
    IntakeItem,
    Job,
    MySqlDomainStore,
)
from .notebook import InMemoryNotebookStore, NotebookService
from .learning import Question, Recommendation, ReviewTask
from .paper_intake import PaperDraft, PaperItem

__all__ = [
    "ErrorEntry",
    "FileRecord",
    "GradeCandidate",
    "IntakeItem",
    "Job",
    "InMemoryNotebookStore",
    "MySqlDomainStore",
    "NotebookService",
    "Question",
    "Recommendation",
    "ReviewTask",
    "PaperDraft",
    "PaperItem",
]
