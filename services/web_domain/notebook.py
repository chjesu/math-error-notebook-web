"""Deterministic application service for the first-error notebook slice."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

from services.web_files import FileIntake

from .mysql_store import ErrorEntry, FileRecord, GradeCandidate, IntakeItem, Job


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    user_id: str
    intake_id: str
    input_version: int
    question_text: str
    answer_text: str
    status: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryNotebookStore:
    """Reference adapter used by unit tests and the localhost demo."""

    def __init__(self) -> None:
        self.files: dict[str, FileRecord] = {}
        self.intakes: dict[str, IntakeItem] = {}
        self.jobs: dict[str, Job] = {}
        self.attempts: dict[str, Attempt] = {}
        self.candidates: dict[str, GradeCandidate] = {}
        self.errors: dict[str, ErrorEntry] = {}
        self._file_keys: dict[tuple[str, str, str], str] = {}
        self._job_keys: dict[tuple[str, str, str], str] = {}
        self._attempt_keys: dict[tuple[str, str], str] = {}

    def create_file(self, *, user_id: str, purpose: str, original_name: str, object_key: str, content_sha256: str, media_type: str, byte_size: int, status: str = "ready") -> FileRecord:
        key = (user_id, purpose, content_sha256)
        existing = self._file_keys.get(key)
        if existing:
            return self.files[existing]
        record = FileRecord(uuid.uuid4().hex, user_id, purpose, object_key, content_sha256, media_type, byte_size, status)
        self.files[record.file_id] = record
        self._file_keys[key] = record.file_id
        return record

    def get_file(self, *, user_id: str, file_id: str) -> FileRecord | None:
        value = self.files.get(file_id)
        return value if value and value.user_id == user_id else None

    def create_intake(self, *, user_id: str, file_id: str, idempotency_key: str) -> tuple[IntakeItem, Job]:
        if not self.get_file(user_id=user_id, file_id=file_id):
            raise LookupError("file not found")
        job_key = (user_id, "extract", idempotency_key)
        existing_job = self._job_keys.get(job_key)
        if existing_job:
            job = self.jobs[existing_job]
            return self.intakes[job.resource_id], job
        intake = IntakeItem(uuid.uuid4().hex, user_id, file_id, 1, "extracting", "", "")
        job = Job(uuid.uuid4().hex, user_id, "extract", intake.intake_id, "queued", None, None)
        self.intakes[intake.intake_id] = intake
        self.jobs[job.job_id] = job
        self._job_keys[job_key] = job.job_id
        return intake, job

    def save_extraction_candidate(self, *, user_id: str, intake_id: str, question_text: str, answer_text: str, evidence: dict[str, Any]) -> IntakeItem:
        del evidence
        current = self._intake(user_id, intake_id)
        if current.status != "extracting":
            raise RuntimeError("conflict")
        updated = IntakeItem(current.intake_id, user_id, current.file_id, current.input_version, "waiting_confirmation", question_text.strip(), answer_text)
        if not updated.question_text:
            raise ValueError("question_text is required")
        self.intakes[intake_id] = updated
        self._update_job(user_id, "extract", intake_id, "waiting_confirmation", {"stage": "candidate_saved"})
        return updated

    def revise_intake(self, *, user_id: str, intake_id: str, expected_version: int, question_text: str, answer_text: str) -> IntakeItem:
        current = self._intake(user_id, intake_id)
        if current.input_version != expected_version:
            raise RuntimeError("input_version_changed")
        if current.status != "waiting_confirmation":
            raise RuntimeError("waiting_confirmation")
        if not question_text.strip():
            raise ValueError("question_text is required")
        updated = IntakeItem(intake_id, user_id, current.file_id, expected_version + 1, current.status, question_text.strip(), answer_text)
        self.intakes[intake_id] = updated
        return updated

    def confirm_intake(self, *, user_id: str, intake_id: str, expected_version: int, idempotency_key: str) -> tuple[str, Job]:
        current = self._intake(user_id, intake_id)
        if current.input_version != expected_version:
            raise RuntimeError("input_version_changed")
        if current.status not in {"waiting_confirmation", "confirmed"}:
            raise RuntimeError("waiting_confirmation")
        key = (user_id, idempotency_key)
        existing_attempt = self._attempt_keys.get(key)
        if existing_attempt:
            job_id = self._job_keys[(user_id, "grade", idempotency_key)]
            return existing_attempt, self.jobs[job_id]
        attempt = Attempt(uuid.uuid4().hex, user_id, intake_id, expected_version, current.question_text, current.answer_text, "grading")
        job = Job(uuid.uuid4().hex, user_id, "grade", attempt.attempt_id, "queued", None, None)
        self.attempts[attempt.attempt_id] = attempt
        self._attempt_keys[key] = attempt.attempt_id
        self.jobs[job.job_id] = job
        self._job_keys[(user_id, "grade", idempotency_key)] = job.job_id
        self.intakes[intake_id] = IntakeItem(current.intake_id, user_id, current.file_id, current.input_version, "confirmed", current.question_text, current.answer_text)
        return attempt.attempt_id, job

    def record_grade_candidate(self, *, user_id: str, attempt_id: str, input_version: int, verdict: str, first_error: str | None, evidence: str | None, confidence: float | None = None) -> GradeCandidate:
        del confidence
        attempt = self.attempts.get(attempt_id)
        if not attempt or attempt.user_id != user_id:
            raise LookupError("attempt not found")
        if attempt.input_version != input_version:
            raise RuntimeError("input_version_changed")
        if verdict not in {"correct", "partial", "incorrect", "unclear"}:
            raise ValueError("unsupported verdict")
        existing = next(
            (
                item
                for item in self.candidates.values()
                if item.attempt_id == attempt_id
                and item.input_version == input_version
                and item.verdict == verdict
                and item.first_error == first_error
                and item.evidence == evidence
            ),
            None,
        )
        if existing:
            return existing
        candidate = GradeCandidate(uuid.uuid4().hex, attempt_id, input_version, verdict, first_error, evidence, "candidate")
        self.candidates[candidate.candidate_id] = candidate
        self.attempts[attempt_id] = Attempt(attempt.attempt_id, user_id, attempt.intake_id, attempt.input_version, attempt.question_text, attempt.answer_text, "grade_ready")
        self._update_job(user_id, "grade", attempt_id, "completed", {"candidate_id": candidate.candidate_id})
        return candidate

    def get_grade_candidate(self, *, user_id: str, candidate_id: str) -> GradeCandidate | None:
        candidate = self.candidates.get(candidate_id)
        if not candidate:
            return None
        attempt = self.attempts.get(candidate.attempt_id)
        return candidate if attempt and attempt.user_id == user_id else None

    def commit_grade(self, *, user_id: str, candidate_id: str, expected_version: int) -> ErrorEntry:
        candidate = self.get_grade_candidate(user_id=user_id, candidate_id=candidate_id)
        if not candidate:
            raise LookupError("grade candidate not found")
        attempt = self.attempts[candidate.attempt_id]
        if candidate.input_version != expected_version:
            raise RuntimeError("input_version_changed")
        if candidate.verdict == "unclear":
            raise RuntimeError("failed_final")
        existing = next((item for item in self.errors.values() if item.user_id == user_id and item.attempt_id == attempt.attempt_id), None)
        if existing:
            return existing
        entry = ErrorEntry(uuid.uuid4().hex, user_id, attempt.attempt_id, attempt.question_text, attempt.answer_text, candidate.first_error, "open", _now())
        self.errors[entry.error_id] = entry
        self.candidates[candidate_id] = GradeCandidate(candidate.candidate_id, candidate.attempt_id, candidate.input_version, candidate.verdict, candidate.first_error, candidate.evidence, "committed")
        self.attempts[attempt.attempt_id] = Attempt(attempt.attempt_id, user_id, attempt.intake_id, attempt.input_version, attempt.question_text, attempt.answer_text, "committed")
        return entry

    def get_job(self, *, user_id: str, job_id: str) -> Job | None:
        value = self.jobs.get(job_id)
        return value if value and value.user_id == user_id else None

    def get_error(self, *, user_id: str, error_id: str) -> ErrorEntry | None:
        value = self.errors.get(error_id)
        return value if value and value.user_id == user_id else None

    def list_errors(self, *, user_id: str) -> list[ErrorEntry]:
        return sorted((item for item in self.errors.values() if item.user_id == user_id and item.status != "removed"), key=lambda item: item.created_at, reverse=True)

    def _intake(self, user_id: str, intake_id: str) -> IntakeItem:
        value = self.intakes.get(intake_id)
        if not value or value.user_id != user_id:
            raise LookupError("intake not found")
        return value

    def _update_job(self, user_id: str, job_type: str, resource_id: str, status: str, checkpoint: dict[str, Any]) -> None:
        for job_id, value in self.jobs.items():
            if value.user_id == user_id and value.job_type == job_type and value.resource_id == resource_id:
                self.jobs[job_id] = Job(job_id, user_id, job_type, resource_id, status, checkpoint, None)
                return


class NotebookService:
    def __init__(self, store: Any, quarantine_root: Path) -> None:
        self.store = store
        self.files = FileIntake(quarantine_root)

    def upload(self, *, user_id: str, purpose: str, original_name: str, content: bytes) -> FileRecord:
        candidate = self.files.quarantine(user_id=user_id, original_name=original_name, content=content)
        return self.store.create_file(
            user_id=user_id,
            purpose=purpose,
            original_name=candidate.original_name,
            object_key=candidate.object_key,
            content_sha256=candidate.content_sha256,
            media_type=candidate.media_type,
            byte_size=candidate.byte_size,
        )
