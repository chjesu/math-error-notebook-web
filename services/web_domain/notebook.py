"""Deterministic application service for the first-error notebook slice."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from copy import deepcopy
from threading import RLock
from io import BytesIO
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
    cross_validate_reference,
    learning_day,
    learning_usage_payload,
    next_review,
    question_match_score,
    rank_questions,
    reference_conflict_resolved,
    reference_adjudication_from_evidence,
    reference_validation_from_evidence,
    review_requires_original,
)
from .mysql_store import ErrorEntry, FileRecord, GradeCandidate, IntakeItem, Job, normalize_extraction_items
from .practice_pdf import build_practice_pdf
from .practice_review import add_practice_calendar, apply_submission, build_manifest, fixed_plan_items, identity_matching_items, legacy_manifest, matching_items, practice_paper_progress, review_locator, shared_review_checkpoints, unresolved_receipt


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
        self._practice_lock = RLock()
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

    def practice_review_context(self, *, user_id: str, attempt_id: str) -> dict | None:
        for candidate in reversed(tuple(self.candidates.values())):
            if candidate.attempt_id != attempt_id or not self.get_grade_candidate(user_id=user_id, candidate_id=candidate.candidate_id):
                continue
            try:
                context = json.loads(candidate.evidence or "{}").get("practice_review")
            except (ValueError, AttributeError):
                continue
            if context:
                return context
        return None

    def list_latest_grade_candidates(self, *, user_id: str) -> list[GradeCandidate]:
        latest: dict[str, GradeCandidate] = {}
        for candidate in reversed(tuple(self.candidates.values())):
            attempt = self.attempts.get(candidate.attempt_id)
            if attempt and attempt.user_id == user_id and candidate.attempt_id not in latest:
                latest[candidate.attempt_id] = candidate
                if len(latest) == 100:
                    break
        return list(latest.values())

    def find_resolved_grade_candidate(self, *, user_id: str, candidate: GradeCandidate) -> GradeCandidate | None:
        validation = reference_validation_from_evidence(candidate.evidence)
        if not validation:
            return None
        for item in reversed(tuple(self.candidates.values())):
            if (item.attempt_id == candidate.attempt_id and item.input_version == candidate.input_version
                    and self.get_grade_candidate(user_id=user_id, candidate_id=item.candidate_id)
                    and reference_conflict_resolved(item.evidence)
                    and reference_validation_from_evidence(item.evidence) == validation):
                return item
        return None

    def find_reference_conflict_candidate(self, *, user_id: str, question_text: str) -> GradeCandidate | None:
        committed_attempts = {item.attempt_id for item in self.errors.values() if item.user_id == user_id}
        resolved_attempts = {
            item.attempt_id
            for item in self.candidates.values()
            if reference_conflict_resolved(item.evidence)
        }
        for candidate in reversed(tuple(self.candidates.values())):
            attempt = self.attempts.get(candidate.attempt_id)
            validation = reference_validation_from_evidence(candidate.evidence)
            if (
                candidate.status == "candidate"
                and attempt is not None
                and attempt.user_id == user_id
                and attempt.attempt_id not in committed_attempts
                and attempt.attempt_id not in resolved_attempts
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
        if self.practice_review_context(user_id=user_id, attempt_id=candidate.attempt_id):
            raise RuntimeError("conflict")
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

    def get_verified_question(self, *, question_id: str) -> VerifiedQuestionReference | None:
        question = self.questions.get(question_id)
        if (
            question is None
            or self.question_rules.get(question_id) not in {("verified", "open", True), ("verified", "user_authorized", True)}
            or not question.answer_text
        ):
            return None
        return VerifiedQuestionReference(
            question_id=question_id,
            version_id=question.version_id or question_id,
            version_no=question.version_no,
            stem_text=question.stem_text,
            answer_text=question.answer_text,
            solution_text=question.solution_text,
            source_title=question.source_title,
            match_score=1.0,
            options=question.options,
        )

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
                    options=question.options,
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
        existing = self.list_recommendations(user_id=user_id, error_id=error_id)
        if len(existing) >= limit:
            return existing[:limit], False
        eligible = [
            question
            for question_id, question in self.questions.items()
            if self.question_rules.get(question_id) in {("verified", "open", True), ("verified", "user_authorized", True)}
            and not any(attempt.user_id == user_id and getattr(attempt, "question_id", None) == question_id for attempt in self.attempts.values())
            and not any(item.user_id == user_id and item.question.question_id == question_id for item in self.recommendations.values())
        ]
        ranked = rank_questions(error.question_text, eligible, limit - len(existing))
        day = learning_day()
        current = self.learning_usage(user_id=user_id)["recommendation"]["count"]
        new_ranked = [
            (question, reason) for question, reason in ranked
            if not any(item.user_id == user_id and item.question.question_id == question.question_id for item in self.recommendations.values())
        ]
        if new_ranked and current >= DAILY_RECOMMENDATION_LIMIT:
            items = self.list_recommendations(user_id=user_id, error_id=error_id)
            return items[:limit], True
        for question, reason in new_ranked[: max(0, DAILY_RECOMMENDATION_LIMIT - current)]:
            existing = next((item for item in self.recommendations.values() if item.user_id == user_id and item.question.question_id == question.question_id), None)
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
        active = [item for item in self.recommendations.values() if item.user_id == user_id and item.status in {"assigned", "completed"}]
        preferred = {
            item.question.question_id: min(
                (candidate for candidate in active if candidate.question.question_id == item.question.question_id),
                key=lambda candidate: (candidate.status != "completed", candidate.recommendation_id),
            ).recommendation_id
            for item in active
        }
        return sorted(
            (item for item in active if item.error_id == error_id and preferred[item.question.question_id] == item.recommendation_id and self.question_rules.get(item.question.question_id) in {("verified", "open", True), ("verified", "user_authorized", True)}),
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

    def list_review_completions(self, *, user_id: str, task_ids: list[str]) -> list[dict]:
        return [{"task_id": item["task_id"], "stage": self.review_tasks[item["task_id"]].stage,
                 "result": item["result"], "completed_at": item["completed_at"]}
                for (owner, _), item in self.review_attempts.items()
                if owner == user_id and item["task_id"] in task_ids and item["task_id"] in self.review_tasks]

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
        today_reviews = [item for item in reviews if item.get("completed_at") and learning_day(item["completed_at"]) == learning_day(current)]
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
            details(error_by_id[item.error_id]) | {"stage": item.stage, "due_at": item.due_at, "status": item.status,
                "task_id": item.task_id, "error_created_at": error_by_id[item.error_id].created_at}
            for item in self.review_tasks.values()
            if item.user_id == user_id and item.error_id in error_by_id
        ]
        attempts = []
        for (owner, _), item in self.review_attempts.items():
            task = self.review_tasks.get(str(item.get("task_id")))
            if owner != user_id or task is None or task.error_id not in error_by_id:
                continue
            attempts.append(details(error_by_id[task.error_id]) | {
                "task_id": task.task_id,
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
            image_object_key = None
            attempt = self.attempts.get(error.attempt_id)
            if attempt:
                intake = self.intakes.get(attempt.intake_id)
                if intake:
                    file = self.files.get(intake.file_id)
                    if file and file.status == "ready":
                        image_object_key = file.object_key
            items.append({"kind": "original", "error_id": error_id, "question_id": None, "stem_text": error.question_text, "answer_text": None, "error_reason": error.first_error, "difficulty": None, "source_title": "个人错题本", "reason": reason, "review_stage": stage, "requires_original": requires_original, "image_object_key": image_object_key})
            recommendations = self.list_recommendations(user_id=user_id, error_id=error_id)
            if not recommendations:
                gaps += 1
            for recommendation in recommendations[:1]:
                question = recommendation.question
                if question.question_id in seen_questions:
                    continue
                seen_questions.add(question.question_id)
                items.append({"kind": "recommendation", "error_id": error_id, "question_id": question.question_id, "stem_text": question.stem_text, "answer_text": question.answer_text, "difficulty": question.difficulty, "source_title": question.source_title, "options": question.options, "reason": recommendation.reason})
        return items, gaps

    def create_practice_job(self, *, user_id: str, error_ids: list[str], idempotency_key: str, include_answers: bool, plan_kind: str = "daily_review") -> Job:
        if not error_ids:
            raise ValueError("error_ids is required")
        for error_id in error_ids:
            if not self.get_error(user_id=user_id, error_id=error_id):
                raise LookupError("error not found")
        key = (user_id, "practice_pdf", idempotency_key)
        digest = hashlib.sha256(json.dumps([sorted(set(error_ids)), include_answers, plan_kind], separators=(",", ":")).encode("ascii")).hexdigest()
        existing = self._job_keys.get(key)
        if existing:
            if self._practice_inputs[key] != digest:
                raise RuntimeError("conflict")
            return self.jobs[existing]
        job = Job(uuid.uuid4().hex, user_id, "practice_pdf", error_ids[0], "queued", {"error_ids": error_ids, "include_answers": include_answers, "plan_kind": plan_kind}, None)
        self.jobs[job.job_id] = job
        self._job_keys[key] = job.job_id
        self._practice_inputs[key] = digest
        return job

    def complete_practice_job(self, *, user_id: str, job_id: str, file_id: str, question_count: int, recommendation_gap_count: int, include_answers: bool, review_manifest: list[dict] | None = None, print_items: list[dict] | None = None, plan_kind: str = "daily_review") -> Job:
        job = self.get_job(user_id=user_id, job_id=job_id)
        if not job or job.job_type != "practice_pdf":
            raise LookupError("job not found")
        checkpoint = dict(job.checkpoint or {}) | {"file_id": file_id, "question_count": question_count, "recommendation_gap_count": recommendation_gap_count, "include_answers": include_answers, "plan_kind": plan_kind, "generated_at": _now().isoformat()}
        if review_manifest is not None:
            checkpoint.update(review_manifest=review_manifest, review_job_id=job_id)
        if print_items is not None:
            checkpoint["print_items"] = deepcopy(print_items)
        completed = Job(job.job_id, user_id, job.job_type, job.resource_id, "completed", checkpoint, None)
        self.jobs[job_id] = completed
        return completed

    def mutate_practice_checkpoint(self, *, user_id: str, job_id: str, operation, share_reviews: bool = False):
        with self._practice_lock:
            job = self.get_job(user_id=user_id, job_id=job_id)
            if not job or job.job_type != "practice_pdf" or job.status != "completed":
                raise LookupError("practice PDF not found")
            checkpoint = deepcopy(job.checkpoint or {})
            if share_reviews:
                papers = self.list_practice_pdfs(user_id=user_id, include_checkpoints=True)
                checkpoints = {paper["task_id"]: paper["_checkpoint"] for paper in papers}
                if job_id not in checkpoints:
                    raise LookupError("practice PDF not found")
                checkpoint = shared_review_checkpoints(checkpoints)[job_id]
            snapshot = deepcopy((self.review_tasks, self.review_attempts, self.errors, self._review_keys))

            def get_task(task_id):
                task = self.review_tasks.get(task_id)
                error = self.get_error(user_id=user_id, error_id=task.error_id) if task and task.user_id == user_id else None
                return task if error and error.status == "open" else None

            def complete(task_id, result, key, now):
                return self.complete_review(user_id=user_id, task_id=task_id, result=result, idempotency_key=key, now=now)

            try:
                result = operation(checkpoint, get_task, complete)
                self.jobs[job_id] = Job(job.job_id, user_id, job.job_type, job.resource_id, job.status, checkpoint, job.last_error_code)
                return result
            except Exception:
                self.review_tasks, self.review_attempts, self.errors, self._review_keys = snapshot
                raise

    def list_practice_pdfs(self, *, user_id: str, include_checkpoints: bool = False) -> list[dict[str, Any]]:
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
                "generated_at": job.checkpoint.get("generated_at"),
                "question_count": int(job.checkpoint.get("question_count", 0)),
                "include_answers": bool(job.checkpoint.get("include_answers", False)),
                "source": str(job.checkpoint.get("source", "generated")),
                "plan_kind": str(job.checkpoint.get("plan_kind", "daily_review")),
                **({"_checkpoint": deepcopy(job.checkpoint)} if include_checkpoints else {}),
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
        self._practice_pdf_lock = RLock()

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

    def list_practice_pdfs(self, *, user_id: str) -> list[dict]:
        return practice_paper_progress(self.store.list_practice_pdfs(user_id=user_id, include_checkpoints=True))

    def review_calendar(self, *, user_id: str, month: str, now: datetime | None = None) -> dict:
        calendar = self.store.review_calendar(user_id=user_id, month=month, now=now)
        return add_practice_calendar(calendar, self.list_practice_pdfs(user_id=user_id))

    def today_practice_plan(self, *, user_id: str, papers: list[dict], now: datetime | None = None) -> dict | None:
        today = learning_day(now or _now())
        papers = [paper for paper in papers if paper.get("source", "generated") == "generated" and paper.get("plan_kind", "daily_review") == "daily_review" and paper.get("generated_at")
                  and learning_day(datetime.fromisoformat(paper["generated_at"])) == today]
        if not papers:
            return None
        paper = max(papers, key=lambda item: (datetime.fromisoformat(item["generated_at"]), item["task_id"]))
        job = self.store.get_job(user_id=user_id, job_id=paper["task_id"])
        if not job or job.status != "completed" or job.job_type != "practice_pdf":
            raise LookupError("practice PDF not found")
        manifest = (job.checkpoint or {}).get("review_manifest") or []
        ids = list({row["task_id"] for row in manifest if row.get("task_id")})
        completions = self.store.list_review_completions(user_id=user_id, task_ids=ids)
        items = fixed_plan_items(manifest, completions, self.store.list_active_reviews(user_id=user_id))
        return {"task_id": job.job_id, "available": bool(manifest), "items": items, "progress": paper.get("progress"),
                "download_url": f"/v1/practice-pdfs/{job.job_id}/download"}

    def create_practice_pdf(self, *, user_id: str, error_ids: list[str], idempotency_key: str, include_answers: bool = False, plan_kind: str = "daily_review") -> Job:
        if plan_kind not in {"daily_review", "practice"}:
            raise ValueError("invalid plan_kind")
        expected_ids = sorted(set(error_ids))
        with self._practice_pdf_lock:
            if plan_kind == "daily_review":
                for paper in self.store.list_practice_pdfs(user_id=user_id, include_checkpoints=True):
                    checkpoint = paper.get("_checkpoint") or {}
                    saved_ids = checkpoint.get("error_ids") or [row.get("error_id") for row in checkpoint.get("review_manifest") or []]
                    generated_at = paper.get("generated_at")
                    if (paper.get("source", "generated") == "generated" and generated_at
                            and learning_day(datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))) == learning_day()
                            and paper.get("plan_kind", "daily_review") == "daily_review"
                            and bool(paper.get("include_answers")) == include_answers
                            and sorted(set(filter(None, saved_ids))) == expected_ids):
                        existing = self.store.get_job(user_id=user_id, job_id=paper["task_id"])
                        if existing:
                            return existing
            job = self.store.create_practice_job(user_id=user_id, error_ids=error_ids, idempotency_key=idempotency_key, include_answers=include_answers, plan_kind=plan_kind)
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
            manifest = build_manifest(job.job_id, items, self.store.list_active_reviews(user_id=user_id))
            print_items = [{key: deepcopy(value) for key, value in item.items() if key != "image_object_key"} for item in items]
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
                review_manifest=manifest,
                print_items=print_items,
                plan_kind=plan_kind,
            )

    def reflow_practice_pdf(self, *, user_id: str, job_id: str) -> Job:
        """Re-render one owned frozen paper without changing questions or review state."""
        with self._practice_pdf_lock:
            job = self.store.get_job(user_id=user_id, job_id=job_id)
            if not job or job.job_type != "practice_pdf" or job.status != "completed" or not job.checkpoint:
                raise LookupError("practice PDF not found")
            checkpoint = deepcopy(job.checkpoint)
            if checkpoint.get("source", "generated") != "generated":
                raise RuntimeError("reflow_snapshot_unavailable")
            print_items = deepcopy(checkpoint.get("print_items") or [])
            if not print_items:
                manifest = checkpoint.get("review_manifest") or []
                error_ids = list(dict.fromkeys(filter(None, checkpoint.get("error_ids") or [row.get("error_id") for row in manifest])))
                current_items, _ = self.store.practice_items(user_id=user_id, error_ids=error_ids)
                print_items = []
                for row in manifest:
                    matched = next((item for item in current_items if item.get("kind") == row.get("kind")
                                    and item.get("error_id") == row.get("error_id")
                                    and item.get("question_id") == row.get("question_id")
                                    and item.get("stem_text") == row.get("stem_text")), None)
                    if matched is None:
                        raise RuntimeError("reflow_snapshot_unavailable")
                    frozen = {key: deepcopy(value) for key, value in matched.items() if key != "image_object_key"}
                    frozen.update(review_code=row.get("code"), review_stage=row.get("stage", 0), requires_original=bool(row.get("required")) if row.get("kind") == "original" else False)
                    print_items.append(frozen)
            content = build_practice_pdf(
                print_items,
                include_answers=bool(checkpoint.get("include_answers", False)),
                asset_root=self.files.root,
                logo_path=Path(__file__).resolve().parents[2] / "assets" / "branding" / "logo-symbol-color-128-v1.png",
            )
            record = self.upload(user_id=user_id, purpose="practice_pdf", original_name=f"practice-{job.job_id[:8]}.pdf", content=content)

            def replace(saved, _get, _complete):
                saved["file_id"] = record.file_id
                saved["print_items"] = deepcopy(print_items)
                saved["reflowed_at"] = _now().isoformat()
                return saved

            self.store.mutate_practice_checkpoint(user_id=user_id, job_id=job_id, operation=replace)
            updated = self.store.get_job(user_id=user_id, job_id=job_id)
            if not updated:
                raise LookupError("practice PDF not found")
            return updated

    def prepare_review_candidate(self, *, user_id: str, candidate: GradeCandidate, locator: dict | None = None) -> GradeCandidate:
        """Carry the frozen adjudication forward without rewriting the OCR input."""
        attempt = self.store.get_attempt(user_id=user_id, attempt_id=candidate.attempt_id)
        if attempt is None:
            raise LookupError("attempt not found")
        if attempt.input_version != candidate.input_version:
            raise RuntimeError("input_version_changed")
        candidate = self.store.find_resolved_grade_candidate(user_id=user_id, candidate=candidate) or candidate
        diagnosis = json.loads(candidate.evidence or "{}")
        context = diagnosis.get("practice_review") or {}
        if not context:
            if locator:
                raise ValueError("only review results may be linked")
            return candidate
        reference = None
        adjudication = reference_adjudication_from_evidence(candidate.evidence)
        if reference_conflict_resolved(candidate.evidence):
            reference = self.store.find_verified_question(question_text=attempt.question_text)
            fresh = cross_validate_reference(reference, "") if reference else {}
            validation = reference_validation_from_evidence(candidate.evidence)
            if any(fresh.get(key) != validation.get(key) for key in ("question_id", "version_id", "reference_answer_sha256")):
                raise RuntimeError("reference_conflict")
        if context.get("status") != "unmatched" and not locator:
            return candidate
        locating = review_locator(locator if locator is not None else context.get("locator"))
        linked = self.resolve_practice_review(user_id=user_id, question_text=attempt.question_text, locator=locating, review_mode=True)
        if (linked.get("status") == "unmatched" and reference is not None
                and adjudication.get("status") == "reference_preferred" and locating.get("kind") != "original"):
            # Only the adjudicated, current reference can bridge a numeric OCR
            # difference, and only to the same recommended question in an owned PDF.
            expected_ids = {reference.question_id}
            if ref := locating.get("question_id"):
                expected_ids = {ref, hashlib.sha256(f"question:{ref}".encode()).hexdigest()[:32]}
            if reference.question_id in expected_ids:
                linked = self.resolve_practice_review(user_id=user_id, question_text=reference.stem_text,
                    locator=locating | {"question_id": reference.question_id, "kind": "recommendation"}, review_mode=True)
        if (linked.get("status") == "matched" and reference is not None
                and adjudication.get("status") == "reference_preferred" and locating.get("kind") != "original"):
            linked["reference_match"] = {key: fresh[key] for key in ("question_id", "version_id", "reference_answer_sha256")}
        if context.get("status") != "unmatched":
            if any(linked.get(key) != context.get(key) for key in ("status", "job_id", "code")):
                raise ValueError("an already linked review cannot be reassigned")
            return candidate
        if linked == context:
            return candidate
        diagnosis["practice_review"] = linked
        return self.store.record_grade_candidate(user_id=user_id, attempt_id=candidate.attempt_id,
            input_version=candidate.input_version, verdict=candidate.verdict, first_error=candidate.first_error,
            evidence=json.dumps(diagnosis, ensure_ascii=False, separators=(",", ":")))

    def resolve_practice_review(self, *, user_id: str, question_text: str, locator: dict | None = None, review_mode: bool = False) -> dict | None:
        locator = review_locator(locator)
        if not locator and not review_mode:
            return None
        papers = self.store.list_practice_pdfs(user_id=user_id)
        if ref := locator.get("pdf_id"):
            papers = [paper for paper in papers if ref in {paper["task_id"], paper["task_id"][:8], paper["filename"]}]
        matches, parsed_bytes = [], 0
        errors, tasks, recs = None, None, {}
        for paper in papers[:100]:
            job = self.store.get_job(user_id=user_id, job_id=paper["task_id"])
            checkpoint = job.checkpoint or {}
            manifest = checkpoint.get("review_manifest")
            if manifest is None and not locator.get("code"):
                # Bounded legacy import; never restore a deleted history job.
                if not paper.get("generated_at") or int(paper["byte_size"]) > 12 * 1024 * 1024 or parsed_bytes > 32 * 1024 * 1024:
                    continue
                _, content = self.download_practice_pdf(user_id=user_id, job_id=job.job_id)
                parsed_bytes += len(content)
                from pypdf import PdfReader
                try:
                    reader = PdfReader(BytesIO(content))
                    if len(reader.pages) > 100:
                        continue
                    text = "\n".join(page.extract_text() or "" for page in reader.pages)
                except Exception:
                    continue
                if errors is None:
                    errors = self.store.list_errors(user_id=user_id)
                    tasks = self.store.list_active_reviews(user_id=user_id)
                    recs = {error.error_id: self.store.list_recommendations(user_id=user_id, error_id=error.error_id) for error in errors}
                generated = datetime.fromisoformat(paper["generated_at"].replace("Z", "+00:00"))
                manifest = legacy_manifest(text, job.job_id, errors, tasks, recs, generated)
                if manifest:
                    def freeze(saved, _get, _complete):
                        saved.setdefault("review_manifest", manifest)
                        saved.setdefault("review_job_id", job.job_id)
                        return saved["review_manifest"]
                    manifest = self.store.mutate_practice_checkpoint(user_id=user_id, job_id=job.job_id, operation=freeze)
            for item in matching_items(manifest or [], locator, question_text, user_id):
                group = [row for row in manifest if row["task_id"] == item["task_id"] and row["required"]]
                signature = (item["task_id"], item["due_at"], item["kind"], item["question_id"], tuple(sorted((row["kind"], row["question_id"] or "") for row in group)))
                matches.append((signature, job.job_id, item, bool(checkpoint.get("review_submissions"))))
        signatures = {match[0] for match in matches}
        if len(signatures) != 1:
            return {"status": "unmatched", "locator": locator}
        # Equivalent reprints share the already-started paper where possible.
        _, job_id, item, _ = max(matches, key=lambda row: row[3])
        return {
            "status": "matched",
            "job_id": job_id,
            "code": item["code"],
            "required": item["required"],
            "error_id": item["error_id"],
            "question_id": item.get("question_id"),
            "kind": item["kind"],
            "stem_text": item["stem_text"],
        }

    def has_practice_review_identity(self, *, user_id: str, question_id: str) -> bool:
        """Bounded ownership check used before treating an ordinary bank item as a PDF review."""
        locator = review_locator({"question_id": question_id, "kind": "recommendation"})
        for paper in self.store.list_practice_pdfs(user_id=user_id)[:100]:
            job = self.store.get_job(user_id=user_id, job_id=paper["task_id"])
            manifest = (job.checkpoint or {}).get("review_manifest") if job else None
            if identity_matching_items(manifest or [], locator, user_id):
                return True
        return False

    def _pending_review_options(self, *, user_id: str, attempt: Attempt, context: dict, diagnosis: dict,
                                papers: list[tuple[dict, Job]]) -> list[dict]:
        locator = review_locator(context.get("locator"))
        validation = diagnosis.get("cross_validation")
        exact_id = str(validation.get("question_id")) if isinstance(validation, dict) and validation.get("status") == "consistent" and validation.get("question_id") else locator.get("question_id")
        exact_locator = {"question_id": exact_id, "kind": "recommendation"} if exact_id else {}
        options = []
        for paper, job in papers:
            checkpoint = job.checkpoint or {}
            manifest = checkpoint.get("review_manifest") or []
            rows = identity_matching_items(manifest, exact_locator, user_id) if exact_locator else []
            if not rows:
                relaxed = {key: value for key, value in locator.items() if key != "code"}
                rows = matching_items(manifest, relaxed, attempt.question_text, user_id)
            for item in rows:
                options.append({
                    "code": item["code"], "pdf_id": job.job_id, "pdf_name": paper["filename"],
                    "error_id": item["error_id"], "question_id": item.get("question_id") or "",
                    "kind": item["kind"], "stage": item["stage"],
                    "generated_at": paper.get("generated_at"),
                    "started": bool(checkpoint.get("review_submissions")),
                })
        unique = {option["code"]: option for option in options}
        return sorted(unique.values(), key=lambda row: (not row["started"], row.get("generated_at") or "", row["code"]))

    def list_pending_practice_review_links(self, *, user_id: str) -> list[dict]:
        items = []
        papers = []
        for paper in self.store.list_practice_pdfs(user_id=user_id)[:100]:
            job = self.store.get_job(user_id=user_id, job_id=paper["task_id"])
            if job:
                papers.append((paper, job))
        for candidate in self.store.list_latest_grade_candidates(user_id=user_id):
            try:
                diagnosis = json.loads(candidate.evidence or "{}")
            except (TypeError, ValueError):
                continue
            context = diagnosis.get("practice_review") or {}
            if context.get("status") != "unmatched":
                continue
            attempt = self.store.get_attempt(user_id=user_id, attempt_id=candidate.attempt_id)
            if not attempt or attempt.input_version != candidate.input_version:
                continue
            items.append({
                "candidate_id": candidate.candidate_id, "input_version": candidate.input_version,
                "verdict": candidate.verdict, "question_text": attempt.question_text,
                "options": self._pending_review_options(
                    user_id=user_id, attempt=attempt, context=context, diagnosis=diagnosis, papers=papers
                ),
            })
        return items

    def link_pending_practice_review(self, *, user_id: str, candidate_id: str, input_version: int, code: str) -> GradeCandidate:
        candidate = self.store.get_grade_candidate(user_id=user_id, candidate_id=candidate_id)
        if not candidate:
            raise LookupError("grade candidate not found")
        if candidate.input_version != input_version:
            raise RuntimeError("input_version_changed")
        pending = next((item for item in self.list_pending_practice_review_links(user_id=user_id)
                        if item["candidate_id"] == candidate_id), None)
        if pending is None:
            raise RuntimeError("conflict")
        option = next((item for item in pending["options"] if item["code"].lower() == code.lower()), None)
        if option is None:
            raise ValueError("invalid review selection")
        diagnosis = json.loads(candidate.evidence or "{}")
        job = self.store.get_job(user_id=user_id, job_id=option["pdf_id"])
        manifest = (job.checkpoint or {}).get("review_manifest") if job else []
        item = next((row for row in manifest or [] if row["code"].lower() == option["code"].lower()), None)
        if item is None:
            raise RuntimeError("conflict")
        diagnosis["practice_review"] = {
            "status": "matched", "job_id": job.job_id, "code": item["code"], "required": item["required"]
        }
        return self.store.record_grade_candidate(
            user_id=user_id, attempt_id=candidate.attempt_id, input_version=candidate.input_version,
            verdict=candidate.verdict, first_error=candidate.first_error,
            evidence=json.dumps(diagnosis, ensure_ascii=False, separators=(",", ":")),
        )

    def commit_practice_review(self, *, user_id: str, candidate: GradeCandidate, now: datetime | None = None) -> dict:
        evidence = json.loads(candidate.evidence or "{}")
        context = evidence.get("practice_review") or {}
        if context.get("status") != "matched":
            return unresolved_receipt("复习判题结果已保留，尚未确认对应 PDF，未重复入本或推进阶段。请补充 PDF 名称，以及图片中的错题编号、阶段或复习码，无需重新上传。")
        current = now or _now()
        def submit(checkpoint, get_task, complete):
            return apply_submission(checkpoint, code=context["code"], candidate_id=candidate.candidate_id,
                                    verdict=candidate.verdict, now=current, get_task=get_task, complete=complete)
        try:
            return self.store.mutate_practice_checkpoint(user_id=user_id, job_id=context["job_id"], operation=submit, share_reviews=True)
        except LookupError as exc:
            if str(exc) != "practice PDF not found":
                raise
            return unresolved_receipt("对应 PDF 历史已不存在，判题结果已保留，未推进复习。请使用当前复习计划。")

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
