"""MySQL 8 store for the user-scoped first-error vertical slice."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Protocol
import uuid


class Cursor(Protocol):
    def execute(self, query: str, args: tuple[Any, ...] = ()) -> int: ...
    def fetchone(self) -> tuple[Any, ...] | None: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...
    def close(self) -> None: ...


class Connection(Protocol):
    def begin(self) -> None: ...
    def cursor(self) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


ConnectionFactory = Callable[[], Connection]


@dataclass(frozen=True)
class FileRecord:
    file_id: str
    user_id: str
    purpose: str
    object_key: str
    content_sha256: str
    media_type: str
    byte_size: int
    status: str


@dataclass(frozen=True)
class IntakeItem:
    intake_id: str
    user_id: str
    file_id: str
    input_version: int
    status: str
    question_text: str
    answer_text: str


@dataclass(frozen=True)
class Job:
    job_id: str
    user_id: str
    job_type: str
    resource_id: str
    status: str
    checkpoint: dict[str, Any] | None
    last_error_code: str | None


@dataclass(frozen=True)
class GradeCandidate:
    candidate_id: str
    attempt_id: str
    input_version: int
    verdict: str
    first_error: str | None
    evidence: str | None
    status: str


@dataclass(frozen=True)
class ErrorEntry:
    error_id: str
    user_id: str
    attempt_id: str
    question_text: str
    answer_text: str
    first_error: str | None
    status: str
    created_at: datetime


def _required(value: str, label: str, maximum: int = 64) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{label} is required and must not exceed {maximum} characters")
    return normalized


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MySqlDomainStore:
    """All personal lookups include the server-resolved user_id."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    def create_file(
        self,
        *,
        user_id: str,
        purpose: str,
        original_name: str,
        object_key: str,
        content_sha256: str,
        media_type: str,
        byte_size: int,
        status: str = "ready",
    ) -> FileRecord:
        user = _required(user_id, "user_id", 32)
        if purpose not in {"exam", "answer_photo", "question_image"}:
            raise ValueError("unsupported upload purpose")
        proposed = uuid.uuid4().hex
        now = _utcnow()
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute("SELECT id FROM web_users WHERE id=%s AND status='active' FOR UPDATE", (user,))
            if not cursor.fetchone():
                raise PermissionError("active account required")
            cursor.execute(
                "INSERT INTO web_files (id,user_id,purpose,original_name,object_key,content_sha256,"
                "media_type,byte_size,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE updated_at=updated_at",
                (proposed, user, purpose, original_name, object_key, content_sha256, media_type, byte_size, status, now, now),
            )
            cursor.execute(
                "SELECT id,user_id,purpose,object_key,content_sha256,media_type,byte_size,status "
                "FROM web_files WHERE user_id=%s AND purpose=%s AND content_sha256=%s FOR UPDATE",
                (user, purpose, content_sha256),
            )
            row = cursor.fetchone()
            if not row:
                raise RuntimeError("file metadata did not persist")
            connection.commit()
            return FileRecord(str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), str(row[5]), int(row[6]), str(row[7]))
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def get_file(self, *, user_id: str, file_id: str) -> FileRecord | None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT id,user_id,purpose,object_key,content_sha256,media_type,byte_size,status "
                "FROM web_files WHERE user_id=%s AND id=%s AND status<>'deleted'",
                (user_id, file_id),
            )
            row = cursor.fetchone()
            return FileRecord(str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), str(row[5]), int(row[6]), str(row[7])) if row else None
        finally:
            cursor.close()
            connection.close()

    def create_intake(
        self, *, user_id: str, file_id: str, idempotency_key: str
    ) -> tuple[IntakeItem, Job]:
        user = _required(user_id, "user_id", 32)
        key = _required(idempotency_key, "idempotency_key")
        intake_id, job_id = uuid.uuid4().hex, uuid.uuid4().hex
        now = _utcnow()
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute("SELECT content_sha256 FROM web_files WHERE id=%s AND user_id=%s AND status='ready' FOR UPDATE", (file_id, user))
            file_row = cursor.fetchone()
            if not file_row:
                raise LookupError("file not found")
            cursor.execute(
                "INSERT INTO intake_items (id,user_id,file_id,item_no,input_version,status,question_text,answer_text,evidence_json,created_at,updated_at) "
                "VALUES (%s,%s,%s,1,1,'extracting',NULL,NULL,JSON_OBJECT(),%s,%s) ON DUPLICATE KEY UPDATE updated_at=updated_at",
                (intake_id, user, file_id, now, now),
            )
            cursor.execute("SELECT id,input_version,status,COALESCE(question_text,''),COALESCE(answer_text,'') FROM intake_items WHERE user_id=%s AND file_id=%s AND item_no=1 FOR UPDATE", (user, file_id))
            intake_row = cursor.fetchone()
            if not intake_row:
                raise RuntimeError("intake did not persist")
            stored_intake_id = str(intake_row[0])
            input_hash = hashlib.sha256(f"{file_row[0]}:1".encode("ascii")).hexdigest()
            cursor.execute(
                "INSERT INTO web_jobs (id,user_id,job_type,resource_type,resource_id,idempotency_key,input_sha256,status,created_at,updated_at) "
                "VALUES (%s,%s,'extract','intake',%s,%s,%s,'queued',%s,%s) ON DUPLICATE KEY UPDATE updated_at=updated_at",
                (job_id, user, stored_intake_id, key, input_hash, now, now),
            )
            cursor.execute("SELECT id,status,checkpoint_json,last_error_code FROM web_jobs WHERE user_id=%s AND job_type='extract' AND idempotency_key=%s FOR UPDATE", (user, key))
            job_row = cursor.fetchone()
            if not job_row:
                raise RuntimeError("job did not persist")
            connection.commit()
            return (
                IntakeItem(stored_intake_id, user, file_id, int(intake_row[1]), str(intake_row[2]), str(intake_row[3]), str(intake_row[4])),
                Job(str(job_row[0]), user, "extract", stored_intake_id, str(job_row[1]), self._json(job_row[2]), job_row[3]),
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def save_extraction_candidate(
        self, *, user_id: str, intake_id: str, question_text: str, answer_text: str, evidence: dict[str, Any]
    ) -> IntakeItem:
        question = _required(question_text, "question_text", 200_000)
        connection = self._connect()
        cursor = connection.cursor()
        now = _utcnow()
        try:
            connection.begin()
            changed = cursor.execute(
                "UPDATE intake_items SET question_text=%s,answer_text=%s,evidence_json=%s,status='waiting_confirmation',updated_at=%s "
                "WHERE id=%s AND user_id=%s AND status='extracting'",
                (question, answer_text, json.dumps(evidence, ensure_ascii=False), now, intake_id, user_id),
            )
            if changed != 1:
                raise LookupError("intake not found")
            cursor.execute("UPDATE web_jobs SET status='waiting_confirmation',checkpoint_json=%s,updated_at=%s WHERE user_id=%s AND resource_type='intake' AND resource_id=%s AND job_type='extract'", (json.dumps({"stage": "candidate_saved"}), now, user_id, intake_id))
            cursor.execute("SELECT file_id,input_version,status,question_text,answer_text FROM intake_items WHERE id=%s AND user_id=%s", (intake_id, user_id))
            row = cursor.fetchone()
            connection.commit()
            return IntakeItem(intake_id, user_id, str(row[0]), int(row[1]), str(row[2]), str(row[3]), str(row[4]))
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def revise_intake(
        self, *, user_id: str, intake_id: str, expected_version: int, question_text: str, answer_text: str
    ) -> IntakeItem:
        question = _required(question_text, "question_text", 200_000)
        connection = self._connect()
        cursor = connection.cursor()
        now = _utcnow()
        try:
            connection.begin()
            changed = cursor.execute(
                "UPDATE intake_items SET question_text=%s,answer_text=%s,input_version=input_version+1,updated_at=%s "
                "WHERE id=%s AND user_id=%s AND input_version=%s AND status='waiting_confirmation'",
                (question, answer_text, now, intake_id, user_id, expected_version),
            )
            if changed != 1:
                cursor.execute("SELECT input_version FROM intake_items WHERE id=%s AND user_id=%s", (intake_id, user_id))
                if cursor.fetchone():
                    raise RuntimeError("input_version_changed")
                raise LookupError("intake not found")
            cursor.execute("SELECT file_id,input_version,status,question_text,answer_text FROM intake_items WHERE id=%s AND user_id=%s", (intake_id, user_id))
            row = cursor.fetchone()
            connection.commit()
            return IntakeItem(intake_id, user_id, str(row[0]), int(row[1]), str(row[2]), str(row[3]), str(row[4]))
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def confirm_intake(
        self, *, user_id: str, intake_id: str, expected_version: int, idempotency_key: str
    ) -> tuple[str, Job]:
        key = _required(idempotency_key, "idempotency_key")
        attempt_id, job_id = uuid.uuid4().hex, uuid.uuid4().hex
        now = _utcnow()
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute(
                "SELECT input_version,status,question_text,answer_text FROM intake_items "
                "WHERE id=%s AND user_id=%s FOR UPDATE",
                (intake_id, user_id),
            )
            intake = cursor.fetchone()
            if not intake:
                raise LookupError("intake not found")
            if int(intake[0]) != expected_version:
                raise RuntimeError("input_version_changed")
            if str(intake[1]) not in {"waiting_confirmation", "confirmed"}:
                raise RuntimeError("waiting_confirmation")
            cursor.execute(
                "INSERT INTO attempts (id,user_id,intake_id,input_version,idempotency_key,question_text,answer_text,status,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'grading',%s,%s) ON DUPLICATE KEY UPDATE updated_at=updated_at",
                (attempt_id, user_id, intake_id, expected_version, key, str(intake[2]), str(intake[3]), now, now),
            )
            cursor.execute("SELECT id,input_version,question_text,answer_text FROM attempts WHERE user_id=%s AND idempotency_key=%s FOR UPDATE", (user_id, key))
            attempt = cursor.fetchone()
            if not attempt:
                raise RuntimeError("attempt did not persist")
            stored_attempt_id = str(attempt[0])
            digest = hashlib.sha256(f"{attempt[1]}:{attempt[2]}:{attempt[3]}".encode("utf-8")).hexdigest()
            cursor.execute(
                "INSERT INTO web_jobs (id,user_id,job_type,resource_type,resource_id,idempotency_key,input_sha256,status,created_at,updated_at) "
                "VALUES (%s,%s,'grade','attempt',%s,%s,%s,'queued',%s,%s) ON DUPLICATE KEY UPDATE updated_at=updated_at",
                (job_id, user_id, stored_attempt_id, key, digest, now, now),
            )
            cursor.execute("SELECT id,status,checkpoint_json,last_error_code FROM web_jobs WHERE user_id=%s AND job_type='grade' AND idempotency_key=%s FOR UPDATE", (user_id, key))
            job_row = cursor.fetchone()
            cursor.execute("UPDATE intake_items SET status='confirmed',updated_at=%s WHERE id=%s AND user_id=%s", (now, intake_id, user_id))
            connection.commit()
            return stored_attempt_id, Job(str(job_row[0]), user_id, "grade", stored_attempt_id, str(job_row[1]), self._json(job_row[2]), job_row[3])
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def record_grade_candidate(
        self,
        *,
        user_id: str,
        attempt_id: str,
        input_version: int,
        verdict: str,
        first_error: str | None,
        evidence: str | None,
        confidence: float | None = None,
    ) -> GradeCandidate:
        if verdict not in {"correct", "partial", "incorrect", "unclear"}:
            raise ValueError("unsupported verdict")
        now = _utcnow()
        result_hash = hashlib.sha256(json.dumps([input_version, verdict, first_error, evidence], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        proposed = uuid.uuid4().hex
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute("SELECT input_version,status FROM attempts WHERE id=%s AND user_id=%s FOR UPDATE", (attempt_id, user_id))
            attempt = cursor.fetchone()
            if not attempt:
                raise LookupError("attempt not found")
            if int(attempt[0]) != input_version:
                raise RuntimeError("input_version_changed")
            cursor.execute(
                "INSERT INTO grade_candidates (id,user_id,attempt_id,input_version,verdict,first_error,evidence_text,confidence,result_sha256,status,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'candidate',%s) ON DUPLICATE KEY UPDATE id=id",
                (proposed, user_id, attempt_id, input_version, verdict, first_error, evidence, confidence, result_hash, now),
            )
            cursor.execute("SELECT id,input_version,verdict,first_error,evidence_text,status FROM grade_candidates WHERE user_id=%s AND attempt_id=%s AND input_version=%s AND result_sha256=%s FOR UPDATE", (user_id, attempt_id, input_version, result_hash))
            row = cursor.fetchone()
            cursor.execute("UPDATE attempts SET status='grade_ready',updated_at=%s WHERE id=%s AND user_id=%s", (now, attempt_id, user_id))
            cursor.execute("UPDATE web_jobs SET status='completed',result_json=%s,updated_at=%s WHERE user_id=%s AND job_type='grade' AND resource_id=%s", (json.dumps({"candidate_id": str(row[0])}), now, user_id, attempt_id))
            connection.commit()
            return GradeCandidate(str(row[0]), attempt_id, int(row[1]), str(row[2]), row[3], row[4], str(row[5]))
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def commit_grade(
        self, *, user_id: str, candidate_id: str, expected_version: int
    ) -> ErrorEntry:
        now = _utcnow()
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute(
                "SELECT c.attempt_id,c.input_version,c.verdict,c.first_error,c.status,a.question_text,a.answer_text "
                "FROM grade_candidates c JOIN attempts a ON a.id=c.attempt_id "
                "WHERE c.id=%s AND c.user_id=%s AND a.user_id=%s FOR UPDATE",
                (candidate_id, user_id, user_id),
            )
            row = cursor.fetchone()
            if not row:
                raise LookupError("grade candidate not found")
            if int(row[1]) != expected_version:
                raise RuntimeError("input_version_changed")
            if str(row[2]) == "unclear":
                raise RuntimeError("failed_final")
            attempt_id = str(row[0])
            cursor.execute("SELECT id,question_text,answer_text,first_error,status,created_at FROM error_notebook_entries WHERE user_id=%s AND attempt_id=%s FOR UPDATE", (user_id, attempt_id))
            existing = cursor.fetchone()
            if existing:
                connection.commit()
                return ErrorEntry(str(existing[0]), user_id, attempt_id, str(existing[1]), str(existing[2]), existing[3], str(existing[4]), existing[5].replace(tzinfo=timezone.utc))
            error_id = uuid.uuid4().hex
            cursor.execute(
                "INSERT INTO error_notebook_entries (id,user_id,attempt_id,grade_candidate_id,question_text,answer_text,first_error,status,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'open',%s,%s)",
                (error_id, user_id, attempt_id, candidate_id, str(row[5]), str(row[6]), row[3], now, now),
            )
            cursor.execute("UPDATE grade_candidates SET status='committed' WHERE id=%s AND user_id=%s", (candidate_id, user_id))
            cursor.execute("UPDATE attempts SET status='committed',updated_at=%s WHERE id=%s AND user_id=%s", (now, attempt_id, user_id))
            cursor.execute("INSERT INTO domain_audit_events (user_id,event_type,resource_type,resource_id,metadata_json,occurred_at) VALUES (%s,'grade.committed','error',%s,%s,%s)", (user_id, error_id, json.dumps({"candidate_id": candidate_id}), now))
            connection.commit()
            return ErrorEntry(error_id, user_id, attempt_id, str(row[5]), str(row[6]), row[3], "open", now.replace(tzinfo=timezone.utc))
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def get_grade_candidate(
        self, *, user_id: str, candidate_id: str
    ) -> GradeCandidate | None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT c.attempt_id,c.input_version,c.verdict,c.first_error,c.evidence_text,c.status "
                "FROM grade_candidates c JOIN attempts a ON a.id=c.attempt_id "
                "WHERE c.id=%s AND c.user_id=%s AND a.user_id=%s",
                (candidate_id, user_id, user_id),
            )
            row = cursor.fetchone()
            return GradeCandidate(candidate_id, str(row[0]), int(row[1]), str(row[2]), row[3], row[4], str(row[5])) if row else None
        finally:
            cursor.close()
            connection.close()

    def list_errors(self, *, user_id: str) -> list[ErrorEntry]:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT id,attempt_id,question_text,answer_text,first_error,status,created_at FROM error_notebook_entries WHERE user_id=%s AND status<>'removed' ORDER BY created_at DESC", (user_id,))
            return [ErrorEntry(str(row[0]), user_id, str(row[1]), str(row[2]), str(row[3]), row[4], str(row[5]), row[6].replace(tzinfo=timezone.utc)) for row in cursor.fetchall()]
        finally:
            cursor.close()
            connection.close()

    def get_job(self, *, user_id: str, job_id: str) -> Job | None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT job_type,resource_id,status,checkpoint_json,last_error_code FROM web_jobs WHERE id=%s AND user_id=%s", (job_id, user_id))
            row = cursor.fetchone()
            return Job(job_id, user_id, str(row[0]), str(row[1]), str(row[2]), self._json(row[3]), row[4]) if row else None
        finally:
            cursor.close()
            connection.close()

    def get_error(self, *, user_id: str, error_id: str) -> ErrorEntry | None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT attempt_id,question_text,answer_text,first_error,status,created_at FROM error_notebook_entries WHERE id=%s AND user_id=%s", (error_id, user_id))
            row = cursor.fetchone()
            return ErrorEntry(error_id, user_id, str(row[0]), str(row[1]), str(row[2]), row[3], str(row[4]), row[5].replace(tzinfo=timezone.utc)) if row else None
        finally:
            cursor.close()
            connection.close()

    @staticmethod
    def _json(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        return json.loads(str(value))
