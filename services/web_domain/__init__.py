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
from .learning import (
    Question,
    Recommendation,
    ReviewTask,
    VerifiedQuestionReference,
    cross_validate_reference,
    reference_validation_from_evidence,
)
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
    "VerifiedQuestionReference",
    "cross_validate_reference",
    "reference_validation_from_evidence",
    "Recommendation",
    "ReviewTask",
    "PaperDraft",
    "PaperItem",
]
