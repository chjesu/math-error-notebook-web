"""Deterministic application service for the first-error notebook slice."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import hashlib
import json
import uuid

from services.web_files import FileIntake

from .learning import (
    DAILY_GRADE_LIMIT,
    DAILY_RECOMMENDATION_LIMIT,
    Question,
    Recommendation,
    ReviewTask,
    VerifiedQuestionReference,
    build_review_calendar,
    learning_day,
    learning_usage_payload,
    next_review,
    question_match_score,
    rank_questions,
    reference_conflict_resolved,
    reference_validation_from_evidence,
    review_requires_original,
)
from .mysql_store import ErrorEntry, FileRecord, GradeCandidate, IntakeItem, Job, normalize_extraction_items
from .practice_pdf import build_practice_pdf


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    user_id: str
    intake_id: str
    input_version: int
    question_text: str
    answer_text: str
    status: str
    question_id: str | None = None


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
        self.questions: dict[str, Question] = {}
        self.question_rules: dict[str, tuple[str, str, bool]] = {}
        self.recommendations: dict[str, Recommendation] = {}
        self.review_tasks: dict[str, ReviewTask] = {}
        self.review_attempts: dict[tuple[str, str], dict[str, Any]] = {}
        self.account_deletions: dict[str, dict[str, Any]] = {}
        self.codex_threads: dict[tuple[str, str], str] = {}
        self.audit_events: list[dict[str, Any]] = []
        self._file_keys: dict[tuple[str, str, str], str] = {}
        self._upload_keys: dict[tuple[str, str], tuple[str, tuple[Any, ...]]] = {}
        self._job_keys: dict[tuple[str, str, str], str] = {}
        self._attempt_keys: dict[tuple[str, str], str] = {}
        self._review_keys: dict[tuple[str, str, int], str] = {}
        self._practice_inputs: dict[tuple[str, str, str], str] = {}
        self.learning_usage_events: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.model_usage_sessions: dict[str, dict[str, Any]] = {}

    def bind_model_session(self, *, user_id: str, session_id: str, now: datetime | None = None) -> None:
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        existing = self.model_usage_sessions.get(session_hash)
        if existing is not None and existing["user_id"] != user_id:
            raise PermissionError("model session already belongs to another user")
        current = now or _now()
        self.model_usage_sessions.setdefault(session_hash, {
            "user_id": user_id,
            "uncached_input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "created_at": current,
            "updated_at": current,
        })

    def model_session_user(self, *, session_id: str) -> str | None:
        record = self.model_usage_sessions.get(hashlib.sha256(session_id.encode("utf-8")).hexdigest())
        return str(record["user_id"]) if record is not None else None

    def record_model_session_usage(
        self,
        *,
        user_id: str,
        session_id: str,
        uncached_input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        now: datetime | None = None,
    ) -> None:
        values = (uncached_input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
            raise ValueError("invalid model token usage")
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        record = self.model_usage_sessions.get(session_hash)
        if record is None or record["user_id"] != user_id:
            raise PermissionError("unbound model session")
        for key, value in zip(("uncached_input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"), values, strict=True):
            record[key] = max(record[key], value)
        record["updated_at"] = now or _now()

    def learning_usage(self, *, user_id: str, now: datetime | None = None) -> dict[str, Any]:
        day = learning_day(now)
        events = [value for (owner, event_day, _kind, _resource), value in self.learning_usage_events.items() if owner == user_id and event_day == day]
        return learning_usage_payload(
            day,
            sum(value["kind"] == "grade" and value["status"] == "counted" for value in events),
            sum(value["kind"] == "recommendation" and value["status"] == "counted" for value in events),
            sum(value["kind"] == "grade" and value["status"] == "reserved" for value in events),
        )

    def reserve_grade_batch(self, *, user_id: str, intake_ids: list[str], now: datetime | None = None) -> None:
        current = now or _now()
        day = learning_day(current)
        resources = list(dict.fromkeys(intake_ids))
        if not resources:
            raise ValueError("intake_ids is required")
        stale_before = current - timedelta(hours=1)
        for key, value in list(self.learning_usage_events.items()):
            if key[0] == user_id and key[1] == day and value["status"] == "reserved" and value["created_at"] < stale_before:
                self.learning_usage_events.pop(key)
        new_resources = [resource for resource in resources if (user_id, day, "grade", resource) not in self.learning_usage_events]
        active = sum(owner == user_id and event_day == day and kind == "grade" for owner, event_day, kind, _resource in self.learning_usage_events)
        if new_resources and active >= DAILY_GRADE_LIMIT:
            raise RuntimeError("daily_grade_limit")
        for resource in new_resources:
            self.learning_usage_events[(user_id, day, "grade", resource)] = {"kind": "grade", "status": "reserved", "created_at": current}

    def finish_grade_usage(self, *, user_id: str, intake_id: str, counted: bool, now: datetime | None = None) -> None:
        day = learning_day(now)
        key = (user_id, day, "grade", intake_id)
        event = self.learning_usage_events.get(key)
        if event is None or event["status"] == "counted":
            return
        if counted:
            event["status"] = "counted"
        else:
            self.learning_usage_events.pop(key, None)

    def get_codex_thread(self, *, user_id: str, conversation_id: str) -> str | None:
        return self.codex_threads.get((user_id, conversation_id))

    def list_recent_codex_threads(self, *, user_id: str, limit: int = 5) -> list[tuple[str, str]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("invalid conversation limit")
        recent = []
        for (owner_id, conversation_id), thread_id in reversed(self.codex_threads.items()):
            if owner_id == user_id:
                recent.append((conversation_id, thread_id))
                if len(recent) == limit:
                    break
        return recent

    def save_codex_thread(self, *, user_id: str, conversation_id: str, thread_id: str) -> str:
        if not user_id or not conversation_id or not thread_id:
            raise ValueError("Codex thread mapping requires all identifiers")
        self.codex_threads.pop((user_id, conversation_id), None)
        self.codex_threads[(user_id, conversation_id)] = thread_id
        return thread_id

    def clear_conversation(self, *, user_id: str) -> None:
        graded_intakes = {
            self.attempts[candidate.attempt_id].intake_id
            for candidate in self.candidates.values()
            if candidate.attempt_id in self.attempts and self.attempts[candidate.attempt_id].user_id == user_id
        }
        active_intakes = {
            item.intake_id for item in self.intakes.values()
            if item.user_id == user_id and (
                item.status in {"extracting", "waiting_confirmation"}
                or item.status == "confirmed" and item.intake_id not in graded_intakes
            )
        }
        for key in [key for key in self.codex_threads if key[0] == user_id]:
            self.codex_threads.pop(key, None)
        for intake_id in active_intakes:
            item = self.intakes[intake_id]
            self.intakes[intake_id] = IntakeItem(item.intake_id, item.user_id, item.file_id, item.input_version, "cancelled", item.question_text, item.answer_text, item.item_no)
        for attempt_id, item in list(self.attempts.items()):
            if item.user_id == user_id and item.status in {"grading", "grade_ready"}:
                self.attempts[attempt_id] = Attempt(item.attempt_id, item.user_id, item.intake_id, item.input_version, item.question_text, item.answer_text, "cancelled", item.question_id)
        for job_id, item in list(self.jobs.items()):
            if item.user_id == user_id and item.job_type in {"extract", "grade"} and item.status not in {"completed", "cancelled", "failed_final"}:
                self.jobs[job_id] = Job(item.job_id, item.user_id, item.job_type, item.resource_id, "cancelled", item.checkpoint, item.last_error_code)
        self.audit_events.append({"user_id": user_id, "event_type": "conversation.cleared", "resource_type": "conversation", "resource_id": user_id, "outcome": "completed"})

    def create_file(self, *, user_id: str, purpose: str, original_name: str, object_key: str, content_sha256: str, media_type: str, byte_size: int, status: str = "ready", idempotency_key: str | None = None) -> FileRecord:
        signature = (purpose, original_name, content_sha256, media_type, byte_size)
        upload_key = (user_id, idempotency_key) if idempotency_key else None
        if upload_key and upload_key in self._upload_keys:
            file_id, original_signature = self._upload_keys[upload_key]
            if original_signature != signature:
                raise RuntimeError("conflict")
            return self.files[file_id]
        key = (user_id, purpose, content_sha256)
        existing = self._file_keys.get(key)
        if existing:
            if upload_key:
                self._upload_keys[upload_key] = (existing, signature)
            return self.files[existing]
        record = FileRecord(uuid.uuid4().hex, user_id, purpose, object_key, content_sha256, media_type, byte_size, status, original_name)
        self.files[record.file_id] = record
        self._file_keys[key] = record.file_id
        if upload_key:
            self._upload_keys[upload_key] = (record.file_id, signature)
        return record

    def get_file(self, *, user_id: str, file_id: str) -> FileRecord | None:
        value = self.files.get(file_id)
        return value if value and value.user_id == user_id and value.status != "deleted" else None

    def get_intake(self, *, user_id: str, intake_id: str) -> IntakeItem | None:
        value = self.intakes.get(intake_id)
        return value if value and value.user_id == user_id else None

    def list_pending_intakes(self, *, user_id: str) -> list[IntakeItem]:
        graded_intakes = {
            self.attempts[candidate.attempt_id].intake_id
            for candidate in self.candidates.values()
            if candidate.attempt_id in self.attempts and self.attempts[candidate.attempt_id].user_id == user_id
        }
        return [
            item for item in self.intakes.values()
            if item.user_id == user_id and (
                item.status in {"extracting", "waiting_confirmation"}
                or item.status == "confirmed" and item.intake_id not in graded_intakes
            )
        ]

    def get_file_intakes(self, *, user_id: str, file_id: str) -> list[IntakeItem]:
        return sorted(
            (item for item in self.intakes.values() if item.user_id == user_id and item.file_id == file_id),
            key=lambda item: item.item_no,
        )

    def get_attempt(self, *, user_id: str, attempt_id: str) -> Attempt | None:
        value = self.attempts.get(attempt_id)
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
        question = question_text.strip()
        if not question:
            raise ValueError("question_text is required")
        if current.status == "waiting_confirmation" and current.question_text == question and current.answer_text == answer_text:
            return current
        if current.status != "extracting":
            raise RuntimeError("conflict")
        updated = IntakeItem(current.intake_id, user_id, current.file_id, current.input_version, "waiting_confirmation", question, answer_text, current.item_no)
        self.intakes[intake_id] = updated
        self._update_job(user_id, "extract", intake_id, "waiting_confirmation", {"stage": "candidate_saved"})
        return updated

    def save_extraction_candidates(
        self, *, user_id: str, intake_id: str, items: list[dict[str, Any]], evidence: dict[str, Any], replace_existing: bool = False
    ) -> list[IntakeItem]:
        del evidence
        values = normalize_extraction_items(items)
        current = self._intake(user_id, intake_id)
        if current.item_no != 1 or current.status != "extracting" and not (replace_existing and current.status == "waiting_confirmation"):
            raise RuntimeError("conflict")
        if current.status == "waiting_confirmation":
            existing = self.get_file_intakes(user_id=user_id, file_id=current.file_id)
            if any(item.status != "waiting_confirmation" for item in existing):
                raise RuntimeError("conflict")
            for item in existing[1:]:
                self.intakes.pop(item.intake_id, None)
        saved = []
        for item in values:
            item_id = intake_id if item["item_no"] == 1 else uuid.uuid4().hex
            value = IntakeItem(
                item_id, user_id, current.file_id, current.input_version if item["item_no"] == 1 else 1,
                "waiting_confirmation", item["question_text"], item["answer_text"], item["item_no"],
            )
            self.intakes[item_id] = value
            saved.append(value)
        self._update_job(user_id, "extract", intake_id, "waiting_confirmation", {"stage": "candidates_saved", "item_count": len(saved)})
        return saved

    def revise_intake(self, *, user_id: str, intake_id: str, expected_version: int, question_text: str, answer_text: str) -> IntakeItem:
        current = self._intake(user_id, intake_id)
        if current.input_version != expected_version:
            raise RuntimeError("input_version_changed")
        if current.status != "waiting_confirmation":
            raise RuntimeError("waiting_confirmation")
        if not question_text.strip():
            raise ValueError("question_text is required")
        updated = IntakeItem(intake_id, user_id, current.file_id, expected_version + 1, current.status, question_text.strip(), answer_text, current.item_no)
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
        self.intakes[intake_id] = IntakeItem(current.intake_id, user_id, current.file_id, current.input_version, "confirmed", current.question_text, current.answer_text, current.item_no)
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
        self.attempts[attempt_id] = Attempt(attempt.attempt_id, user_id, attempt.intake_id, attempt.input_version, attempt.question_text, attempt.answer_text, "grade_ready", attempt.question_id)
        self._update_job(user_id, "grade", attempt_id, "completed", {"candidate_id": candidate.candidate_id})
        return candidate

    def get_grade_candidate(self, *, user_id: str, candidate_id: str) -> GradeCandidate | None:
        candidate = self.candidates.get(candidate_id)
        if not candidate:
            return None
        attempt = self.attempts.get(candidate.attempt_id)
        return candidate if attempt and attempt.user_id == user_id else None

    def find_reference_conflict_candidate(self, *, user_id: str, question_text: str) -> GradeCandidate | None:
        committed_attempts = {item.attempt_id for item in self.errors.values() if item.user_id == user_id}
        for candidate in reversed(tuple(self.candidates.values())):
            attempt = self.attempts.get(candidate.attempt_id)
            validation = reference_validation_from_evidence(candidate.evidence)
            if (
                candidate.status == "candidate"
                and attempt is not None
                and attempt.user_id == user_id
                and attempt.attempt_id not in committed_attempts
                and validation is not None
                and validation.get("status") == "conflict"
                and not reference_conflict_resolved(candidate.evidence)
                and (attempt.question_text == question_text or question_match_score(attempt.question_text, question_text) >= 0.92)
            ):
                return candidate
        return None

    def commit_grade(self, *, user_id: str, candidate_id: str, expected_version: int) -> ErrorEntry:
        candidate = self.get_grade_candidate(user_id=user_id, candidate_id=candidate_id)
        if not candidate:
            raise LookupError("grade candidate not found")
        attempt = self.attempts[candidate.attempt_id]
        if candidate.input_version != expected_version:
            raise RuntimeError("input_version_changed")
        if candidate.verdict not in {"partial", "incorrect"}:
            raise RuntimeError("failed_final")
        validation = reference_validation_from_evidence(candidate.evidence)
        if validation and validation.get("status") == "conflict" and not reference_conflict_resolved(candidate.evidence):
            raise RuntimeError("reference_conflict")
        existing = next((item for item in self.errors.values() if item.user_id == user_id and item.attempt_id == attempt.attempt_id), None)
        if existing:
            self._ensure_review(user_id, existing.error_id)
            return existing
        entry = ErrorEntry(uuid.uuid4().hex, user_id, attempt.attempt_id, attempt.question_text, attempt.answer_text, candidate.first_error, "open", _now(), candidate.evidence, attempt.question_id)
        self.errors[entry.error_id] = entry
        self.candidates[candidate_id] = GradeCandidate(candidate.candidate_id, candidate.attempt_id, candidate.input_version, candidate.verdict, candidate.first_error, candidate.evidence, "committed")
        self.attempts[attempt.attempt_id] = Attempt(attempt.attempt_id, user_id, attempt.intake_id, attempt.input_version, attempt.question_text, attempt.answer_text, "committed", attempt.question_id)
        self._ensure_review(user_id, entry.error_id)
        return entry

    def add_question(self, question: Question, *, status: str = "verified", license_status: str = "open", verified: bool = True) -> None:
        self.questions[question.question_id] = question
        self.question_rules[question.question_id] = (status, license_status, verified)

    def find_verified_question(self, *, question_text: str) -> VerifiedQuestionReference | None:
        matches: list[VerifiedQuestionReference] = []
        for question_id, question in self.questions.items():
            if self.question_rules.get(question_id) not in {("verified", "open", True), ("verified", "user_authorized", True)}:
                continue
            score = question_match_score(question_text, question.stem_text)
            if score >= 0.92 and question.answer_text:
                matches.append(VerifiedQuestionReference(
                    question_id=question_id,
                    version_id=question.version_id or question_id,
                    version_no=question.version_no,
                    stem_text=question.stem_text,
                    answer_text=question.answer_text,
                    solution_text=question.solution_text,
                    source_title=question.source_title,
                    match_score=score,
                ))
        return sorted(matches, key=lambda value: (-value.match_score, value.question_id))[0] if matches else None

    def link_attempt_question(self, *, user_id: str, attempt_id: str, question_id: str) -> Attempt:
        attempt = self.get_attempt(user_id=user_id, attempt_id=attempt_id)
        if not attempt:
            raise LookupError("attempt not found")
        if self.question_rules.get(question_id) not in {("verified", "open", True), ("verified", "user_authorized", True)}:
            raise LookupError("verified question not found")
        if attempt.question_id not in {None, question_id}:
            raise RuntimeError("question_link_conflict")
        linked = Attempt(
            attempt.attempt_id, attempt.user_id, attempt.intake_id, attempt.input_version,
            attempt.question_text, attempt.answer_text, attempt.status, question_id,
        )
        self.attempts[attempt_id] = linked
        return linked

    def assign_recommendations(self, *, user_id: str, error_id: str, limit: int = 2) -> tuple[list[Recommendation], bool]:
        error = self.get_error(user_id=user_id, error_id=error_id)
        if not error:
            raise LookupError("error not found")
        eligible = [
            question
            for question_id, question in self.questions.items()
            if self.question_rules.get(question_id) in {("verified", "open", True), ("verified", "user_authorized", True)}
            and not any(attempt.user_id == user_id and getattr(attempt, "question_id", None) == question_id for attempt in self.attempts.values())
        ]
        ranked = rank_questions(error.question_text, eligible, limit)
        day = learning_day()
        current = self.learning_usage(user_id=user_id)["recommendation"]["count"]
        new_ranked = [
            (question, reason) for question, reason in ranked
            if not any(item.user_id == user_id and item.error_id == error_id and item.question.question_id == question.question_id for item in self.recommendations.values())
        ]
        if new_ranked and current >= DAILY_RECOMMENDATION_LIMIT:
            items = self.list_recommendations(user_id=user_id, error_id=error_id)
            return items[:limit], True
        for question, reason in new_ranked[: max(0, DAILY_RECOMMENDATION_LIMIT - current)]:
            existing = next((item for item in self.recommendations.values() if item.user_id == user_id and item.error_id == error_id and item.question.question_id == question.question_id), None)
            if not existing:
                existing = Recommendation(uuid.uuid4().hex, user_id, error_id, question, reason, "assigned")
                self.recommendations[existing.recommendation_id] = existing
                resource = hashlib.sha256(f"{error_id}:{question.question_id}".encode("ascii")).hexdigest()
                self.learning_usage_events[(user_id, day, "recommendation", resource)] = {"kind": "recommendation", "status": "counted", "created_at": _now()}
        items = self.list_recommendations(user_id=user_id, error_id=error_id)
        return items[:limit], len(items) < limit

    def list_recommendations(self, *, user_id: str, error_id: str) -> list[Recommendation]:
        if not self.get_error(user_id=user_id, error_id=error_id):
            raise LookupError("error not found")
        return sorted(
            (item for item in self.recommendations.values() if item.user_id == user_id and item.error_id == error_id and item.status in {"assigned", "completed"} and self.question_rules.get(item.question.question_id) in {("verified", "open", True), ("verified", "user_authorized", True)}),
            key=lambda item: item.recommendation_id,
        )

    def list_due_reviews(self, *, user_id: str, now: datetime | None = None) -> list[ReviewTask]:
        current = now or _now()
        return sorted(
            (ReviewTask(item.task_id, item.user_id, item.error_id, item.stage, item.due_at, "ready") for item in self.review_tasks.values() if item.user_id == user_id and item.status in {"pending", "ready"} and item.due_at <= current),
            key=lambda item: (item.due_at, item.error_id),
        )

    def list_active_reviews(self, *, user_id: str) -> list[ReviewTask]:
        return sorted(
            (item for item in self.review_tasks.values() if item.user_id == user_id and item.status in {"pending", "ready"}),
            key=lambda item: (item.due_at, item.error_id),
        )

    def complete_review(self, *, user_id: str, task_id: str, result: str, idempotency_key: str, now: datetime | None = None) -> ReviewTask | None:
        key = (user_id, idempotency_key)
        if key in self.review_attempts:
            next_id = self.review_attempts[key]["next_task_id"]
            return self.review_tasks.get(next_id) if next_id else None
        task = self.review_tasks.get(task_id)
        completed_at = now or _now()
        if not task or task.user_id != user_id:
            raise LookupError("review task not found")
        if task.status not in {"pending", "ready"} or task.due_at > completed_at:
            raise RuntimeError("conflict")
        target = next_review(task.stage, result, completed_at)
        self.review_tasks[task_id] = ReviewTask(task.task_id, user_id, task.error_id, task.stage, task.due_at, "completed")
        for existing_id, existing in list(self.review_tasks.items()):
            if existing.user_id == user_id and existing.error_id == task.error_id and existing.status in {"pending", "ready"}:
                self.review_tasks[existing_id] = ReviewTask(existing.task_id, user_id, existing.error_id, existing.stage, existing.due_at, "cancelled")
        next_task = None
        if target is None:
            error = self.errors[task.error_id]
            self.errors[task.error_id] = ErrorEntry(error.error_id, user_id, error.attempt_id, error.question_text, error.answer_text, error.first_error, "mastered", error.created_at, error.evidence, error.question_id)
        else:
            stage, due_at = target
            review_key = (user_id, task.error_id, stage)
            next_id = self._review_keys.get(review_key, uuid.uuid4().hex)
            next_task = ReviewTask(next_id, user_id, task.error_id, stage, due_at, "ready" if due_at <= completed_at else "pending")
            self.review_tasks[next_id] = next_task
            self._review_keys[review_key] = next_id
        self.review_attempts[key] = {"task_id": task_id, "result": result, "next_task_id": next_task.task_id if next_task else None, "completed_at": completed_at}
        return next_task

    def progress(self, *, user_id: str, now: datetime | None = None) -> dict[str, Any]:
        current = now or _now()
        errors = [item for item in self.errors.values() if item.user_id == user_id and item.status != "removed"]
        due = self.list_due_reviews(user_id=user_id, now=current)
        active_reviews = self.list_active_reviews(user_id=user_id)
        stage_counts = {str(stage): sum(item.stage == stage for item in active_reviews) for stage in range(1, 7)}
        gaps = sum(not any(rec.user_id == user_id and rec.error_id == item.error_id and rec.status == "assigned" for rec in self.recommendations.values()) for item in errors)
        reviews = [item for (owner, _), item in self.review_attempts.items() if owner == user_id]
        today_reviews = [item for item in reviews if item.get("completed_at") and item["completed_at"].date() == current.date()]
        correct = sum(item["result"] == "correct" for item in reviews)
        return {"error_count": len(errors), "mastered_count": sum(item.status == "mastered" for item in errors), "due_review_count": len(due), "recommendation_gap_count": gaps, "completed_review_count": len(reviews), "correct_review_count": correct, "partial_review_count": sum(item["result"] == "partial" for item in reviews), "wrong_review_count": sum(item["result"] == "wrong" for item in reviews), "review_accuracy_percent": round(correct * 100 / len(reviews)) if reviews else 0, "review_stage_counts": stage_counts, "today_completed_review_count": len(today_reviews), "today_needs_correction_count": sum(item["result"] in {"partial", "wrong"} for item in today_reviews), "sample_sufficient": len(errors) >= 3}

    def review_calendar(self, *, user_id: str, month: str, now: datetime | None = None) -> dict[str, object]:
        errors = [item for item in self.errors.values() if item.user_id == user_id and item.status != "removed"]

        def details(error: ErrorEntry) -> dict[str, object]:
            return {
                "error_id": error.error_id,
                "question_text": error.question_text,
                "first_error": error.first_error,
                "evidence": error.evidence,
            }

        error_by_id = {item.error_id: item for item in errors}
        tasks = [
            details(error_by_id[item.error_id]) | {"stage": item.stage, "due_at": item.due_at, "status": item.status}
            for item in self.review_tasks.values()
            if item.user_id == user_id and item.error_id in error_by_id
        ]
        attempts = []
        for (owner, _), item in self.review_attempts.items():
            task = self.review_tasks.get(str(item.get("task_id")))
            if owner != user_id or task is None or task.error_id not in error_by_id:
                continue
            attempts.append(details(error_by_id[task.error_id]) | {
                "stage": task.stage,
                "result": item["result"],
                "completed_at": item["completed_at"],
                "status": "completed",
            })
        return build_review_calendar(
            month,
            errors=[details(item) | {"created_at": item.created_at, "status": item.status} for item in errors],
            review_tasks=tasks,
            review_attempts=attempts,
            total_error_count=len(errors),
            now=now,
        )

    def bank_status(self) -> dict[str, int]:
        recommendable = sum(self.question_rules.get(question_id) in {("verified", "open", True), ("verified", "user_authorized", True)} for question_id in self.questions)
        candidate = sum(self.question_rules.get(question_id, ("candidate", "restricted", False))[0] == "candidate" for question_id in self.questions)
        return {"question_count": len(self.questions), "recommendable_count": recommendable, "candidate_count": candidate}

    def set_error_status(self, *, user_id: str, error_id: str, status: str) -> ErrorEntry:
        if status not in {"mastered", "removed"}:
            raise ValueError("unsupported error status")
        error = self.get_error(user_id=user_id, error_id=error_id)
        if not error or error.status == "removed":
            raise LookupError("error not found")
        updated = ErrorEntry(error.error_id, user_id, error.attempt_id, error.question_text, error.answer_text, error.first_error, status, error.created_at, error.evidence, error.question_id)
        self.errors[error_id] = updated
        for task_id, task in list(self.review_tasks.items()):
            if task.user_id == user_id and task.error_id == error_id and task.status in {"pending", "ready"}:
                self.review_tasks[task_id] = ReviewTask(task.task_id, user_id, error_id, task.stage, task.due_at, "cancelled")
        if status == "removed":
            for recommendation_id, item in list(self.recommendations.items()):
                if item.user_id == user_id and item.error_id == error_id and item.status in {"candidate", "assigned"}:
                    self.recommendations[recommendation_id] = Recommendation(item.recommendation_id, user_id, error_id, item.question, item.reason, "withdrawn")
        return updated

    def pending_job_count(self, *, user_id: str) -> int:
        return sum(job.user_id == user_id and job.status not in {"completed", "cancelled", "failed_final"} for job in self.jobs.values())

    def practice_items(self, *, user_id: str, error_ids: list[str]) -> tuple[list[dict[str, Any]], int]:
        items: list[dict[str, Any]] = []
        gaps = 0
        seen_questions: set[str] = set()
        active_reviews = {item.error_id: item for item in self.list_active_reviews(user_id=user_id)}
        for error_id in error_ids:
            error = self.get_error(user_id=user_id, error_id=error_id)
            if not error:
                raise LookupError("error not found")
            attempts = []
            for (owner, _), attempt in self.review_attempts.items():
                task = self.review_tasks.get(str(attempt.get("task_id")))
                if owner == user_id and task and task.error_id == error_id:
                    attempts.append(attempt)
            latest_result = max(attempts, key=lambda value: value["completed_at"])["result"] if attempts else None
            stage = active_reviews[error_id].stage if error_id in active_reviews else (6 if error.status == "mastered" else 1)
            requires_original = review_requires_original(stage, latest_result)
            reason = "订正回退" if latest_result in {"partial", "wrong"} else f"第 {stage} 阶段"
            items.append({"kind": "original", "error_id": error_id, "question_id": None, "stem_text": error.question_text, "answer_text": None, "error_reason": error.first_error, "difficulty": None, "source_title": "个人错题本", "reason": reason, "review_stage": stage, "requires_original": requires_original})
            recommendations = self.list_recommendations(user_id=user_id, error_id=error_id)
            if not recommendations:
                gaps += 1
            for recommendation in recommendations[:1]:
                question = recommendation.question
                if question.question_id in seen_questions:
                    continue
                seen_questions.add(question.question_id)
                items.append({"kind": "recommendation", "error_id": error_id, "question_id": question.question_id, "stem_text": question.stem_text, "answer_text": question.answer_text, "difficulty": question.difficulty, "source_title": question.source_title, "reason": recommendation.reason})
        return items, gaps

    def create_practice_job(self, *, user_id: str, error_ids: list[str], idempotency_key: str, include_answers: bool) -> Job:
        if not error_ids:
            raise ValueError("error_ids is required")
        for error_id in error_ids:
            if not self.get_error(user_id=user_id, error_id=error_id):
                raise LookupError("error not found")
        key = (user_id, "practice_pdf", idempotency_key)
        digest = hashlib.sha256(json.dumps([sorted(set(error_ids)), include_answers], separators=(",", ":")).encode("ascii")).hexdigest()
        existing = self._job_keys.get(key)
        if existing:
            if self._practice_inputs[key] != digest:
                raise RuntimeError("conflict")
            return self.jobs[existing]
        job = Job(uuid.uuid4().hex, user_id, "practice_pdf", error_ids[0], "queued", {"error_ids": error_ids, "include_answers": include_answers}, None)
        self.jobs[job.job_id] = job
        self._job_keys[key] = job.job_id
        self._practice_inputs[key] = digest
        return job

    def complete_practice_job(self, *, user_id: str, job_id: str, file_id: str, question_count: int, recommendation_gap_count: int, include_answers: bool) -> Job:
        job = self.get_job(user_id=user_id, job_id=job_id)
        if not job or job.job_type != "practice_pdf":
            raise LookupError("job not found")
        completed = Job(job.job_id, user_id, job.job_type, job.resource_id, "completed", {"file_id": file_id, "question_count": question_count, "recommendation_gap_count": recommendation_gap_count, "include_answers": include_answers}, None)
        self.jobs[job_id] = completed
        return completed

    def list_practice_pdfs(self, *, user_id: str) -> list[dict[str, Any]]:
        items = []
        for job in reversed(self.jobs.values()):
            if job.user_id != user_id or job.job_type != "practice_pdf" or job.status != "completed" or not job.checkpoint:
                continue
            record = self.files.get(str(job.checkpoint.get("file_id", "")))
            if not record or record.user_id != user_id or record.purpose != "practice_pdf" or record.status != "ready":
                continue
            items.append({
                "task_id": job.job_id,
                "filename": record.original_name,
                "byte_size": record.byte_size,
                "generated_at": None,
                "question_count": int(job.checkpoint.get("question_count", 0)),
                "include_answers": bool(job.checkpoint.get("include_answers", False)),
            })
        return items

    def create_export_job(self, *, user_id: str, idempotency_key: str, expires_at: datetime) -> Job:
        key = (user_id, "export", idempotency_key)
        existing = self._job_keys.get(key)
        if existing:
            return self.jobs[existing]
        job = Job(uuid.uuid4().hex, user_id, "export", "export", "queued", {"expires_at": expires_at.isoformat()}, None)
        self.jobs[job.job_id] = job
        self._job_keys[key] = job.job_id
        return job

    def complete_export_job(self, *, user_id: str, job_id: str, file_id: str, expires_at: datetime) -> Job:
        job = self.get_job(user_id=user_id, job_id=job_id)
        if not job or job.job_type != "export":
            raise LookupError("export not found")
        completed = Job(job.job_id, user_id, "export", job.resource_id, "completed", {"file_id": file_id, "expires_at": expires_at.isoformat()}, None)
        self.jobs[job_id] = completed
        return completed

    def claim_export_download(self, *, user_id: str, job_id: str, maximum: int) -> bool:
        job = self.get_job(user_id=user_id, job_id=job_id)
        if not job or job.job_type != "export":
            raise LookupError("export not found")
        completed = sum(1 for event in self.audit_events if event["user_id"] == user_id and event["resource_id"] == job_id and event["event_type"] == "export.downloaded")
        allowed = completed < maximum
        self.audit_events.append({"user_id": user_id, "event_type": "export.downloaded" if allowed else "export.download_denied", "resource_type": "export", "resource_id": job_id, "outcome": "allowed" if allowed else "download_limit"})
        return allowed

    def export_data(self, *, user_id: str) -> dict[str, Any]:
        files = [self._file_export(item) for item in self.files.values() if item.user_id == user_id]
        intakes = [self._intake_export(item) for item in self.intakes.values() if item.user_id == user_id]
        attempts = [self._attempt_export(item) for item in self.attempts.values() if item.user_id == user_id]
        candidates = [self._candidate_export(item) for item in self.candidates.values() if (attempt := self.attempts.get(item.attempt_id)) and attempt.user_id == user_id]
        errors = [self._error_export(item) for item in self.errors.values() if item.user_id == user_id]
        recommendations = [self._recommendation_export(item) for item in self.recommendations.values() if item.user_id == user_id]
        learning_usage = [
            {"date": day, "kind": kind, "resource_id": resource, "status": value["status"], "created_at": value["created_at"]}
            for (owner, day, kind, resource), value in self.learning_usage_events.items() if owner == user_id
        ]
        reviews = [self._review_export(item) for item in self.review_tasks.values() if item.user_id == user_id]
        review_attempts = [dict(item) for (owner, _), item in self.review_attempts.items() if owner == user_id]
        jobs = [self._job_export(item) for item in self.jobs.values() if item.user_id == user_id and item.job_type != "export"]
        return {"schema_version": 2, "files": files, "intakes": intakes, "attempts": attempts, "grade_candidates": candidates, "errors": errors, "recommendations": recommendations, "learning_usage": learning_usage, "review_tasks": reviews, "review_attempts": review_attempts, "jobs": jobs}

    def deactivate_user_data(self, *, user_id: str) -> None:
        for file_id, item in list(self.files.items()):
            if item.user_id == user_id and item.status != "deleted":
                self.files[file_id] = FileRecord(item.file_id, item.user_id, item.purpose, item.object_key, item.content_sha256, item.media_type, item.byte_size, "deleted", item.original_name)
        for job_id, item in list(self.jobs.items()):
            if item.user_id == user_id and item.status not in {"completed", "cancelled", "failed_final"}:
                self.jobs[job_id] = Job(item.job_id, item.user_id, item.job_type, item.resource_id, "cancelled", item.checkpoint, item.last_error_code)
        for intake_id, item in list(self.intakes.items()):
            if item.user_id == user_id and item.status in {"extracting", "waiting_confirmation", "confirmed"}:
                self.intakes[intake_id] = IntakeItem(item.intake_id, item.user_id, item.file_id, item.input_version, "cancelled", item.question_text, item.answer_text, item.item_no)
        for attempt_id, item in list(self.attempts.items()):
            if item.user_id == user_id and item.status in {"grading", "grade_ready"}:
                self.attempts[attempt_id] = Attempt(item.attempt_id, item.user_id, item.intake_id, item.input_version, item.question_text, item.answer_text, "cancelled", item.question_id)
        for task_id, item in list(self.review_tasks.items()):
            if item.user_id == user_id and item.status in {"pending", "ready"}:
                self.review_tasks[task_id] = ReviewTask(item.task_id, item.user_id, item.error_id, item.stage, item.due_at, "cancelled")
        for recommendation_id, item in list(self.recommendations.items()):
            if item.user_id == user_id and item.status in {"candidate", "assigned", "completed"}:
                self.recommendations[recommendation_id] = Recommendation(item.recommendation_id, item.user_id, item.error_id, item.question, item.reason, "withdrawn")
        for error_id, item in list(self.errors.items()):
            if item.user_id == user_id and item.status != "removed":
                self.errors[error_id] = ErrorEntry(item.error_id, item.user_id, item.attempt_id, item.question_text, item.answer_text, item.first_error, "removed", item.created_at, item.evidence, item.question_id)

    @staticmethod
    def _file_export(item: FileRecord) -> dict[str, Any]:
        return {"file_id": item.file_id, "purpose": item.purpose, "original_name": item.original_name, "media_type": item.media_type, "byte_size": item.byte_size, "status": item.status}

    @staticmethod
    def _intake_export(item: IntakeItem) -> dict[str, Any]:
        return {"intake_id": item.intake_id, "file_id": item.file_id, "input_version": item.input_version, "status": item.status, "question_text": item.question_text, "answer_text": item.answer_text}

    @staticmethod
    def _attempt_export(item: Attempt) -> dict[str, Any]:
        return {"attempt_id": item.attempt_id, "intake_id": item.intake_id, "question_id": item.question_id, "input_version": item.input_version, "question_text": item.question_text, "answer_text": item.answer_text, "status": item.status}

    @staticmethod
    def _candidate_export(item: GradeCandidate) -> dict[str, Any]:
        return {"candidate_id": item.candidate_id, "attempt_id": item.attempt_id, "input_version": item.input_version, "verdict": item.verdict, "first_error": item.first_error, "evidence": item.evidence, "status": item.status}

    @staticmethod
    def _error_export(item: ErrorEntry) -> dict[str, Any]:
        return {"error_id": item.error_id, "attempt_id": item.attempt_id, "question_text": item.question_text, "answer_text": item.answer_text, "first_error": item.first_error, "evidence": item.evidence, "status": item.status, "created_at": item.created_at}

    @staticmethod
    def _recommendation_export(item: Recommendation) -> dict[str, Any]:
        return {"recommendation_id": item.recommendation_id, "error_id": item.error_id, "question_id": item.question.question_id, "reason": item.reason, "status": item.status}

    @staticmethod
    def _review_export(item: ReviewTask) -> dict[str, Any]:
        return {"review_id": item.task_id, "error_id": item.error_id, "stage": item.stage, "due_at": item.due_at, "status": item.status}

    @staticmethod
    def _job_export(item: Job) -> dict[str, Any]:
        return {"job_id": item.job_id, "job_type": item.job_type, "resource_id": item.resource_id, "status": item.status}

    def begin_user_deletion(self, *, user_id: str) -> dict[str, Any]:
        value = self.account_deletions.get(user_id)
        if value:
            return dict(value)
        value = {"status": "pending", "updated_at": _now(), "last_error_code": None}
        self.account_deletions[user_id] = value
        return dict(value)

    def deletion_status(self, *, user_id: str) -> dict[str, Any] | None:
        value = self.account_deletions.get(user_id)
        return dict(value) if value else None

    def deletion_file_keys(self, *, user_id: str) -> list[str]:
        return [item.object_key for item in self.files.values() if item.user_id == user_id and item.status == "deleted"]

    def pending_deletion_user_ids(self) -> list[str]:
        return sorted(user_id for user_id, value in self.account_deletions.items() if value["status"] == "pending")

    def complete_user_deletion(self, *, user_id: str) -> dict[str, Any]:
        value = self.begin_user_deletion(user_id=user_id)
        if value["status"] != "completed":
            value = {"status": "completed", "updated_at": _now(), "last_error_code": None}
            self.account_deletions[user_id] = value
        return dict(value)

    def record_deletion_error(self, *, user_id: str, code: str) -> None:
        self.begin_user_deletion(user_id=user_id)
        self.account_deletions[user_id] = {"status": "pending", "updated_at": _now(), "last_error_code": code[:64]}

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

    def _ensure_review(self, user_id: str, error_id: str) -> ReviewTask:
        key = (user_id, error_id, 1)
        task_id = self._review_keys.get(key)
        if task_id:
            return self.review_tasks[task_id]
        task = ReviewTask(uuid.uuid4().hex, user_id, error_id, 1, _now(), "ready")
        self.review_tasks[task.task_id] = task
        self._review_keys[key] = task.task_id
        return task


class NotebookService:
    EXPORT_TTL = timedelta(hours=24)
    EXPORT_MAX_DOWNLOADS = 3

    def __init__(self, store: Any, quarantine_root: Path) -> None:
        self.store = store
        self.files = FileIntake(quarantine_root)

    def upload(self, *, user_id: str, purpose: str, original_name: str, content: bytes, idempotency_key: str | None = None) -> FileRecord:
        candidate = self.files.quarantine(user_id=user_id, original_name=original_name, content=content)
        try:
            record = self.store.create_file(
                user_id=user_id,
                purpose=purpose,
                original_name=candidate.original_name,
                object_key=candidate.object_key,
                content_sha256=candidate.content_sha256,
                media_type=candidate.media_type,
                byte_size=candidate.byte_size,
                idempotency_key=idempotency_key,
            )
        except Exception:
            candidate.local_path.unlink(missing_ok=True)
            raise
        if record.object_key != candidate.object_key:
            candidate.local_path.unlink(missing_ok=True)
        return record

    def clear_conversation(self, *, user_id: str) -> None:
        self.store.clear_conversation(user_id=user_id)

    def create_practice_pdf(self, *, user_id: str, error_ids: list[str], idempotency_key: str, include_answers: bool = False) -> Job:
        job = self.store.create_practice_job(user_id=user_id, error_ids=error_ids, idempotency_key=idempotency_key, include_answers=include_answers)
        if job.status == "completed":
            return job
        items, gaps = self.store.practice_items(user_id=user_id, error_ids=error_ids)
        recommendations_by_error = {error_id: 0 for error_id in error_ids}
        for item in items:
            if item["kind"] == "recommendation":
                recommendations_by_error[str(item["error_id"])] += 1
        task_count = sum(item["kind"] == "recommendation" for item in items) + sum(
            bool(item.get("requires_original")) or recommendations_by_error[str(item["error_id"])] == 0
            for item in items if item["kind"] == "original"
        )
        content = build_practice_pdf(
            items,
            include_answers=include_answers,
            asset_root=self.files.root,
            logo_path=Path(__file__).resolve().parents[2] / "assets" / "branding" / "logo-symbol-color-128-v1.png",
        )
        record = self.upload(user_id=user_id, purpose="practice_pdf", original_name=f"practice-{job.job_id[:8]}.pdf", content=content)
        return self.store.complete_practice_job(
            user_id=user_id,
            job_id=job.job_id,
            file_id=record.file_id,
            question_count=task_count,
            recommendation_gap_count=gaps,
            include_answers=include_answers,
        )

    def download_practice_pdf(self, *, user_id: str, job_id: str) -> tuple[str, bytes]:
        job = self.store.get_job(user_id=user_id, job_id=job_id)
        if not job or job.job_type != "practice_pdf" or job.status != "completed" or not job.checkpoint:
            raise LookupError("practice PDF not found")
        record = self.store.get_file(user_id=user_id, file_id=str(job.checkpoint["file_id"]))
        if not record or record.purpose != "practice_pdf":
            raise LookupError("practice PDF not found")
        content = self.files.read(record.object_key)
        if hashlib.sha256(content).hexdigest() != record.content_sha256:
            raise RuntimeError("file_integrity_failed")
        return f"practice-{job.job_id[:8]}.pdf", content

    def read_intake_source(self, *, user_id: str, intake_id: str) -> tuple[str, str, bytes]:
        intake = self.store.get_intake(user_id=user_id, intake_id=intake_id)
        if not intake or intake.status == "cancelled":
            raise LookupError("intake source not found")
        record = self.store.get_file(user_id=user_id, file_id=intake.file_id)
        if not record or record.purpose != "question_image" or record.media_type not in {"image/jpeg", "image/png"}:
            raise LookupError("intake source not found")
        content = self.files.read(record.object_key)
        if hashlib.sha256(content).hexdigest() != record.content_sha256:
            raise RuntimeError("file_integrity_failed")
        return record.original_name, record.media_type, content

    def create_export(self, *, user_id: str, idempotency_key: str, now: datetime | None = None) -> Job:
        created_at = now or _now()
        expires_at = created_at + self.EXPORT_TTL
        job = self.store.create_export_job(user_id=user_id, idempotency_key=idempotency_key, expires_at=expires_at)
        if job.status == "completed":
            return job
        data = self.store.export_data(user_id=user_id)
        content = json.dumps(
            {"schema_version": 1, "exported_at": created_at.isoformat(), "data": self._export_value(data)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        record = self._store_export(user_id=user_id, job_id=job.job_id, content=content)
        return self.store.complete_export_job(user_id=user_id, job_id=job.job_id, file_id=record.file_id, expires_at=expires_at)

    def download_export(self, *, user_id: str, job_id: str, now: datetime | None = None) -> tuple[str, bytes]:
        job = self.store.get_job(user_id=user_id, job_id=job_id)
        if not job or job.job_type != "export" or job.status != "completed" or not job.checkpoint:
            raise LookupError("export not found")
        expires_at = datetime.fromisoformat(str(job.checkpoint["expires_at"]))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= (now or _now()):
            raise LookupError("export expired")
        record = self.store.get_file(user_id=user_id, file_id=str(job.checkpoint["file_id"]))
        if not record or record.purpose != "export":
            raise LookupError("export not found")
        content = self.files.read(record.object_key)
        if hashlib.sha256(content).hexdigest() != record.content_sha256:
            raise RuntimeError("file_integrity_failed")
        if not self.store.claim_export_download(user_id=user_id, job_id=job_id, maximum=self.EXPORT_MAX_DOWNLOADS):
            raise LookupError("export download limit reached")
        return f"export-{job.job_id[:8]}.json", content

    def deactivate_user_data(self, *, user_id: str) -> None:
        self.prepare_user_deletion(user_id=user_id)

    def prepare_user_deletion(self, *, user_id: str) -> dict[str, Any]:
        """Persist pending, then make business rows inaccessible; safe to retry after auth failure."""
        self.store.begin_user_deletion(user_id=user_id)
        try:
            self.store.deactivate_user_data(user_id=user_id)
        except Exception:
            self.store.record_deletion_error(user_id=user_id, code="domain_cleanup_failed")
            raise
        return self.deletion_status(user_id=user_id) or {"status": "pending", "updated_at": None, "last_error_code": None}

    def deletion_status(self, *, user_id: str) -> dict[str, Any] | None:
        return self.store.deletion_status(user_id=user_id)

    def pending_deletion_user_ids(self) -> list[str]:
        return self.store.pending_deletion_user_ids()

    def record_deletion_error(self, *, user_id: str, code: str) -> None:
        self.store.record_deletion_error(user_id=user_id, code=code)

    def complete_user_deletion(self, *, user_id: str) -> dict[str, Any]:
        try:
            for object_key in self.store.deletion_file_keys(user_id=user_id):
                target = (self.files.root / object_key).resolve()
                if self.files.root not in target.parents:
                    raise ValueError("unsafe deletion path")
                target.unlink(missing_ok=True)
            return self.store.complete_user_deletion(user_id=user_id)
        except Exception:
            self.store.record_deletion_error(user_id=user_id, code="file_purge_failed")
            raise

    def _store_export(self, *, user_id: str, job_id: str, content: bytes) -> FileRecord:
        namespace = hashlib.sha256(user_id.encode("ascii")).hexdigest()[:16]
        object_key = f"quarantine/{namespace}/export-{job_id}.json"
        target = (self.files.root / object_key).resolve()
        if self.files.root not in target.parents:
            raise ValueError("unsafe export path")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            if target.read_bytes() != content:
                raise RuntimeError("export object collision")
        return self.store.create_file(
            user_id=user_id,
            purpose="export",
            original_name=f"export-{job_id[:8]}.json",
            object_key=object_key,
            content_sha256=hashlib.sha256(content).hexdigest(),
            media_type="application/json",
            byte_size=len(content),
        )

    @staticmethod
    def _export_value(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if is_dataclass(value):
            return {key: NotebookService._export_value(item) for key, item in asdict(value).items()}
        if isinstance(value, dict):
            return {str(key): NotebookService._export_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [NotebookService._export_value(item) for item in value]
        return value
