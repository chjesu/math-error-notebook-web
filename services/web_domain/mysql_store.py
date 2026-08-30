"""MySQL 8 store for the user-scoped first-error vertical slice."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Protocol
import uuid

from .learning import (
    DAILY_GRADE_LIMIT,
    DAILY_RECOMMENDATION_LIMIT,
    Question,
    Recommendation,
    ReviewTask,
    VerifiedQuestionReference,
    build_review_calendar,
    calendar_month_range,
    learning_day,
    learning_usage_payload,
    next_review,
    question_anchor,
    question_match_score,
    rank_questions,
    reference_conflict_resolved,
    reference_validation_from_evidence,
    review_requires_original,
)


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
    original_name: str = ""


@dataclass(frozen=True)
class IntakeItem:
    intake_id: str
    user_id: str
    file_id: str
    input_version: int
    status: str
    question_text: str
    answer_text: str
    item_no: int = 1


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
    evidence: str | None = None
    question_id: str | None = None


def _required(value: str, label: str, maximum: int = 64) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{label} is required and must not exceed {maximum} characters")
    return normalized


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_extraction_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise ValueError("one to twenty extraction items are required")
    normalized = []
    for expected_item_no, item in enumerate(items, 1):
        if not isinstance(item, dict) or item.get("item_no") != expected_item_no:
            raise ValueError("extraction item numbers must be sequential")
        question = _required(item.get("question_text", ""), "question_text", 200_000)
        answer = item.get("answer_text", "")
        if not isinstance(answer, str) or len(answer) > 200_000:
            raise ValueError("answer_text must be a string of at most 200000 characters")
        normalized.append({"item_no": expected_item_no, "question_text": question, "answer_text": answer.strip()})
    return normalized


class MySqlDomainStore:
    """All personal lookups include the server-resolved user_id."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    def bind_model_session(self, *, user_id: str, session_id: str, now: datetime | None = None) -> None:
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute("SELECT user_id FROM model_usage_sessions WHERE session_hash=%s FOR UPDATE", (session_hash,))
            row = cursor.fetchone()
            if row is not None and str(row[0]) != user_id:
                raise PermissionError("model session already belongs to another user")
            if row is None:
                cursor.execute(
                    "INSERT INTO model_usage_sessions "
                    "(session_hash,user_id,uncached_input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,created_at,updated_at) "
                    "VALUES (%s,%s,0,0,0,0,%s,%s)",
                    (session_hash, user_id, current, current),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def model_session_user(self, *, session_id: str) -> str | None:
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT user_id FROM model_usage_sessions WHERE session_hash=%s", (session_hash,))
            row = cursor.fetchone()
            return str(row[0]) if row is not None else None
        finally:
            cursor.close()
            connection.close()

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
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute("SELECT user_id FROM model_usage_sessions WHERE session_hash=%s FOR UPDATE", (session_hash,))
            row = cursor.fetchone()
            if row is None or str(row[0]) != user_id:
                raise PermissionError("unbound model session")
            cursor.execute(
                "UPDATE model_usage_sessions SET "
                "uncached_input_tokens=GREATEST(uncached_input_tokens,%s),"
                "output_tokens=GREATEST(output_tokens,%s),"
                "cache_read_tokens=GREATEST(cache_read_tokens,%s),"
                "cache_write_tokens=GREATEST(cache_write_tokens,%s),updated_at=%s "
                "WHERE session_hash=%s AND user_id=%s",
                (*values, current, session_hash, user_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def learning_usage(self, *, user_id: str, now: datetime | None = None) -> dict[str, Any]:
        day = learning_day(now)
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT kind,status,COUNT(*) FROM daily_learning_usage WHERE user_id=%s AND usage_date=%s GROUP BY kind,status",
                (user_id, day),
            )
            counts = {(str(kind), str(status)): int(count) for kind, status, count in cursor.fetchall()}
            return learning_usage_payload(
                day,
                counts.get(("grade", "counted"), 0),
                counts.get(("recommendation", "counted"), 0),
                counts.get(("grade", "reserved"), 0),
            )
        finally:
            cursor.close()
            connection.close()

    def reserve_grade_batch(self, *, user_id: str, intake_ids: list[str], now: datetime | None = None) -> None:
        resources = list(dict.fromkeys(intake_ids))
        if not resources:
            raise ValueError("intake_ids is required")
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
        day = learning_day(now)
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute("SELECT id FROM web_users WHERE id=%s FOR UPDATE", (user_id,))
            if not cursor.fetchone():
                raise LookupError("user not found")
            cursor.execute(
                "DELETE FROM daily_learning_usage WHERE user_id=%s AND usage_date=%s AND kind='grade' AND status='reserved' AND created_at<%s",
                (user_id, day, current - timedelta(hours=1)),
            )
            placeholders = ",".join(["%s"] * len(resources))
            cursor.execute(
                f"SELECT resource_id FROM daily_learning_usage WHERE user_id=%s AND usage_date=%s AND kind='grade' AND resource_id IN ({placeholders})",
                (user_id, day, *resources),
            )
            existing = {str(row[0]) for row in cursor.fetchall()}
            new_resources = [resource for resource in resources if resource not in existing]
            cursor.execute("SELECT COUNT(*) FROM daily_learning_usage WHERE user_id=%s AND usage_date=%s AND kind='grade'", (user_id, day))
            active = int((cursor.fetchone() or (0,))[0])
            if new_resources and active >= DAILY_GRADE_LIMIT:
                raise RuntimeError("daily_grade_limit")
            for resource in new_resources:
                cursor.execute(
                    "INSERT INTO daily_learning_usage (user_id,usage_date,kind,resource_id,status,created_at,updated_at) VALUES (%s,%s,'grade',%s,'reserved',%s,%s)",
                    (user_id, day, resource, current, current),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def finish_grade_usage(self, *, user_id: str, intake_id: str, counted: bool, now: datetime | None = None) -> None:
        day = learning_day(now)
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            if counted:
                cursor.execute(
                    "UPDATE daily_learning_usage SET status='counted',updated_at=%s WHERE user_id=%s AND usage_date=%s AND kind='grade' AND resource_id=%s AND status='reserved'",
                    (current, user_id, day, intake_id),
                )
            else:
                cursor.execute(
                    "DELETE FROM daily_learning_usage WHERE user_id=%s AND usage_date=%s AND kind='grade' AND resource_id=%s AND status='reserved'",
                    (user_id, day, intake_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def get_codex_thread(self, *, user_id: str, conversation_id: str) -> str | None:
        user = _required(user_id, "user_id", 32)
        conversation = _required(conversation_id, "conversation_id", 32)
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT thread_id FROM codex_conversations WHERE user_id=%s AND conversation_id=%s",
                (user, conversation),
            )
            row = cursor.fetchone()
            return str(row[0]) if row else None
        finally:
            cursor.close()
            connection.close()

    def list_recent_codex_threads(self, *, user_id: str, limit: int = 5) -> list[tuple[str, str]]:
        user = _required(user_id, "user_id", 32)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("invalid conversation limit")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT conversation_id,thread_id FROM codex_conversations "
                "WHERE user_id=%s ORDER BY updated_at DESC LIMIT %s",
                (user, limit),
            )
            return [(str(row[0]), str(row[1])) for row in cursor.fetchall()]
        finally:
            cursor.close()
            connection.close()

    def save_codex_thread(self, *, user_id: str, conversation_id: str, thread_id: str) -> str:
        user = _required(user_id, "user_id", 32)
        conversation = _required(conversation_id, "conversation_id", 32)
        thread = _required(thread_id, "thread_id", 128)
        now = _utcnow()
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO codex_conversations (user_id,conversation_id,thread_id,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE thread_id=VALUES(thread_id),updated_at=VALUES(updated_at)",
                (user, conversation, thread, now, now),
            )
            connection.commit()
            return thread
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def clear_conversation(self, *, user_id: str) -> None:
        user = _required(user_id, "user_id", 32)
        connection = self._connect()
        cursor = connection.cursor()
        now = _utcnow()
        try:
            connection.begin()
            cursor.execute("DELETE FROM codex_conversations WHERE user_id=%s", (user,))
            cursor.execute("UPDATE web_jobs SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,updated_at=%s WHERE user_id=%s AND job_type IN ('extract','grade') AND status IN ('queued','running','waiting_confirmation','failed_retryable')", (now, user))
            cursor.execute("UPDATE attempts SET status='cancelled',updated_at=%s WHERE user_id=%s AND status='grading'", (now, user))
            cursor.execute(
                "UPDATE intake_items i SET i.status='cancelled',i.updated_at=%s WHERE i.user_id=%s AND (i.status IN ('extracting','waiting_confirmation') OR (i.status='confirmed' AND NOT EXISTS ("
                "SELECT 1 FROM attempts a JOIN grade_candidates c ON c.attempt_id=a.id AND c.user_id=a.user_id WHERE a.user_id=%s AND a.intake_id=i.id)))",
                (now, user, user),
            )
            cursor.execute("INSERT INTO domain_audit_events (user_id,event_type,resource_type,resource_id,metadata_json,occurred_at) VALUES (%s,'conversation.cleared','conversation',%s,%s,%s)", (user, user, json.dumps({"outcome": "completed"}), now))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

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
        idempotency_key: str | None = None,
    ) -> FileRecord:
        user = _required(user_id, "user_id", 32)
        if purpose not in {"exam", "answer_photo", "question_image", "practice_pdf", "export"}:
            raise ValueError("unsupported upload purpose")
        proposed = uuid.uuid4().hex
        now = _utcnow()
        request_digest = hashlib.sha256(json.dumps([purpose, original_name, content_sha256, media_type, byte_size], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute("SELECT id FROM web_users WHERE id=%s AND status='active' FOR UPDATE", (user,))
            if not cursor.fetchone():
                raise PermissionError("active account required")
            if idempotency_key:
                key = _required(idempotency_key, "idempotency_key")
                cursor.execute(
                    "SELECT i.request_sha256,f.id,f.user_id,f.purpose,f.object_key,f.content_sha256,f.media_type,f.byte_size,f.status,f.original_name "
                    "FROM file_upload_idempotency i JOIN web_files f ON f.id=i.file_id "
                    "WHERE i.user_id=%s AND i.idempotency_key=%s FOR UPDATE",
                    (user, key),
                )
                previous = cursor.fetchone()
                if previous:
                    if str(previous[0]) != request_digest:
                        raise RuntimeError("conflict")
                    connection.commit()
                    return FileRecord(str(previous[1]), str(previous[2]), str(previous[3]), str(previous[4]), str(previous[5]), str(previous[6]), int(previous[7]), str(previous[8]), str(previous[9]))
            cursor.execute(
                "INSERT INTO web_files (id,user_id,purpose,original_name,object_key,content_sha256,"
                "media_type,byte_size,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE updated_at=updated_at",
                (proposed, user, purpose, original_name, object_key, content_sha256, media_type, byte_size, status, now, now),
            )
            cursor.execute(
                "SELECT id,user_id,purpose,object_key,content_sha256,media_type,byte_size,status,original_name "
                "FROM web_files WHERE user_id=%s AND purpose=%s AND content_sha256=%s FOR UPDATE",
                (user, purpose, content_sha256),
            )
            row = cursor.fetchone()
            if not row:
                raise RuntimeError("file metadata did not persist")
            if idempotency_key:
                cursor.execute(
                    "INSERT INTO file_upload_idempotency (user_id,idempotency_key,request_sha256,file_id,created_at) VALUES (%s,%s,%s,%s,%s)",
                    (user, key, request_digest, str(row[0]), now),
                )
            connection.commit()
            return FileRecord(str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), str(row[5]), int(row[6]), str(row[7]), str(row[8]))
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
                "SELECT id,user_id,purpose,object_key,content_sha256,media_type,byte_size,status,original_name "
                "FROM web_files WHERE user_id=%s AND id=%s AND status<>'deleted'",
                (user_id, file_id),
            )
            row = cursor.fetchone()
            return FileRecord(str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), str(row[5]), int(row[6]), str(row[7]), str(row[8])) if row else None
        finally:
            cursor.close()
            connection.close()

    def get_intake(self, *, user_id: str, intake_id: str) -> IntakeItem | None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT file_id,input_version,status,COALESCE(question_text,''),COALESCE(answer_text,''),item_no "
                "FROM intake_items WHERE id=%s AND user_id=%s",
                (intake_id, user_id),
            )
            row = cursor.fetchone()
            return IntakeItem(intake_id, user_id, str(row[0]), int(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5])) if row else None
        finally:
            cursor.close()
            connection.close()

    def list_pending_intakes(self, *, user_id: str) -> list[IntakeItem]:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT id,file_id,input_version,status,COALESCE(question_text,''),COALESCE(answer_text,''),item_no "
                "FROM intake_items i WHERE i.user_id=%s AND (i.status IN ('extracting','waiting_confirmation') OR (i.status='confirmed' AND NOT EXISTS ("
                "SELECT 1 FROM attempts a JOIN grade_candidates c ON c.attempt_id=a.id AND c.user_id=a.user_id "
                "WHERE a.user_id=%s AND a.intake_id=i.id))) ORDER BY created_at,item_no",
                (user_id, user_id),
            )
            return [
                IntakeItem(str(row[0]), user_id, str(row[1]), int(row[2]), str(row[3]), str(row[4]), str(row[5]), int(row[6]))
                for row in cursor.fetchall()
            ]
        finally:
            cursor.close()
            connection.close()

    def get_file_intakes(self, *, user_id: str, file_id: str) -> list[IntakeItem]:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT id,input_version,status,COALESCE(question_text,''),COALESCE(answer_text,''),item_no "
                "FROM intake_items WHERE file_id=%s AND user_id=%s ORDER BY item_no",
                (file_id, user_id),
            )
            return [
                IntakeItem(str(row[0]), user_id, file_id, int(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5]))
                for row in cursor.fetchall()
            ]
        finally:
            cursor.close()
            connection.close()

    def get_attempt(self, *, user_id: str, attempt_id: str) -> Any | None:
        from .notebook import Attempt

        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT intake_id,input_version,question_text,answer_text,status,question_id "
                "FROM attempts WHERE id=%s AND user_id=%s",
                (attempt_id, user_id),
            )
            row = cursor.fetchone()
            return Attempt(attempt_id, user_id, str(row[0]), int(row[1]), str(row[2]), str(row[3]), str(row[4]), str(row[5]) if row[5] else None) if row else None
        finally:
            cursor.close()
            connection.close()

    def find_verified_question(self, *, question_text: str) -> VerifiedQuestionReference | None:
        # Ponytail: use the current verified/version/license indexes before adding a second search service.
        anchor = question_anchor(question_text)
        if not anchor:
            return None
        escaped = anchor.replace("=", "==").replace("%", "=%").replace("_", "=_")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT q.id,v.id,v.version_no,v.stem_text,v.answer_text,v.solution_text,s.title "
                "FROM questions q JOIN question_sources s ON s.id=q.source_id "
                "JOIN question_versions v ON v.question_id=q.id AND v.version_no=q.current_version_no "
                "WHERE q.status='verified' AND s.license_status IN ('open','user_authorized') "
                "AND v.answer_text IS NOT NULL AND v.stem_text LIKE %s ESCAPE '=' "
                "AND EXISTS (SELECT 1 FROM question_verifications x WHERE x.question_version_id=v.id AND x.verdict='verified') "
                "ORDER BY q.id LIMIT 1000",
                (f"%{escaped}%",),
            )
            matches = []
            for row in cursor.fetchall():
                score = question_match_score(question_text, str(row[3]))
                if score >= 0.92:
                    matches.append(VerifiedQuestionReference(
                        question_id=str(row[0]), version_id=str(row[1]), version_no=int(row[2]),
                        stem_text=str(row[3]), answer_text=str(row[4]), solution_text=row[5],
                        source_title=str(row[6]), match_score=score,
                    ))
            return sorted(matches, key=lambda value: (-value.match_score, value.question_id))[0] if matches else None
        finally:
            cursor.close()
            connection.close()

    def link_attempt_question(self, *, user_id: str, attempt_id: str, question_id: str) -> Any:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute(
                "SELECT q.id FROM questions q JOIN question_sources s ON s.id=q.source_id "
                "JOIN question_versions v ON v.question_id=q.id AND v.version_no=q.current_version_no "
                "WHERE q.id=%s AND q.status='verified' AND s.license_status IN ('open','user_authorized') "
                "AND EXISTS (SELECT 1 FROM question_verifications x WHERE x.question_version_id=v.id AND x.verdict='verified') FOR UPDATE",
                (question_id,),
            )
            if not cursor.fetchone():
                raise LookupError("verified question not found")
            cursor.execute("SELECT question_id FROM attempts WHERE id=%s AND user_id=%s FOR UPDATE", (attempt_id, user_id))
            row = cursor.fetchone()
            if not row:
                raise LookupError("attempt not found")
            if row[0] and str(row[0]) != question_id:
                raise RuntimeError("question_link_conflict")
            cursor.execute("UPDATE attempts SET question_id=%s,updated_at=%s WHERE id=%s AND user_id=%s", (question_id, _utcnow(), attempt_id, user_id))
            connection.commit()
            return self.get_attempt(user_id=user_id, attempt_id=attempt_id)
        except Exception:
            connection.rollback()
            raise
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
            cursor.execute(
                "SELECT file_id,input_version,status,question_text,answer_text FROM intake_items WHERE id=%s AND user_id=%s FOR UPDATE",
                (intake_id, user_id),
            )
            row = cursor.fetchone()
            if not row:
                raise LookupError("intake not found")
            if str(row[2]) == "waiting_confirmation" and str(row[3]) == question and str(row[4]) == answer_text:
                connection.commit()
                return IntakeItem(intake_id, user_id, str(row[0]), int(row[1]), str(row[2]), str(row[3]), str(row[4]))
            if str(row[2]) != "extracting":
                raise RuntimeError("conflict")
            cursor.execute(
                "UPDATE intake_items SET question_text=%s,answer_text=%s,evidence_json=%s,status='waiting_confirmation',updated_at=%s "
                "WHERE id=%s AND user_id=%s AND status='extracting'",
                (question, answer_text, json.dumps(evidence, ensure_ascii=False), now, intake_id, user_id),
            )
            cursor.execute("UPDATE web_jobs SET status='waiting_confirmation',checkpoint_json=%s,updated_at=%s WHERE user_id=%s AND resource_type='intake' AND resource_id=%s AND job_type='extract'", (json.dumps({"stage": "candidate_saved"}), now, user_id, intake_id))
            cursor.execute("SELECT file_id,input_version,status,question_text,answer_text,item_no FROM intake_items WHERE id=%s AND user_id=%s", (intake_id, user_id))
            row = cursor.fetchone()
            connection.commit()
            return IntakeItem(intake_id, user_id, str(row[0]), int(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5]))
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def save_extraction_candidates(
        self, *, user_id: str, intake_id: str, items: list[dict[str, Any]], evidence: dict[str, Any], replace_existing: bool = False
    ) -> list[IntakeItem]:
        values = normalize_extraction_items(items)
        connection = self._connect()
        cursor = connection.cursor()
        now = _utcnow()
        try:
            connection.begin()
            cursor.execute(
                "SELECT file_id,input_version,status FROM intake_items WHERE id=%s AND user_id=%s AND item_no=1 FOR UPDATE",
                (intake_id, user_id),
            )
            primary = cursor.fetchone()
            if not primary:
                raise LookupError("intake not found")
            current_status = str(primary[2])
            if current_status == "waiting_confirmation" and replace_existing:
                cursor.execute(
                    "SELECT status FROM intake_items WHERE file_id=%s AND user_id=%s FOR UPDATE",
                    (str(primary[0]), user_id),
                )
                if any(str(row[0]) != "waiting_confirmation" for row in cursor.fetchall()):
                    raise RuntimeError("conflict")
                cursor.execute("DELETE FROM intake_items WHERE file_id=%s AND user_id=%s AND item_no>1", (str(primary[0]), user_id))
            elif current_status != "extracting":
                raise RuntimeError("conflict")
            file_id, input_version = str(primary[0]), int(primary[1])
            encoded_evidence = json.dumps(evidence, ensure_ascii=False)
            first = values[0]
            cursor.execute(
                "UPDATE intake_items SET question_text=%s,answer_text=%s,evidence_json=%s,status='waiting_confirmation',updated_at=%s "
                "WHERE id=%s AND user_id=%s AND status IN ('extracting','waiting_confirmation')",
                (first["question_text"], first["answer_text"], encoded_evidence, now, intake_id, user_id),
            )
            ids = [intake_id]
            for item in values[1:]:
                child_id = uuid.uuid4().hex
                cursor.execute(
                    "INSERT INTO intake_items (id,user_id,file_id,item_no,input_version,status,question_text,answer_text,evidence_json,created_at,updated_at) "
                    "VALUES (%s,%s,%s,%s,1,'waiting_confirmation',%s,%s,%s,%s,%s)",
                    (child_id, user_id, file_id, item["item_no"], item["question_text"], item["answer_text"], encoded_evidence, now, now),
                )
                ids.append(child_id)
            cursor.execute(
                "UPDATE web_jobs SET status='waiting_confirmation',checkpoint_json=%s,updated_at=%s "
                "WHERE user_id=%s AND resource_type='intake' AND resource_id=%s AND job_type='extract'",
                (json.dumps({"stage": "candidates_saved", "item_count": len(values)}), now, user_id, intake_id),
            )
            connection.commit()
            return [
                IntakeItem(item_id, user_id, file_id, input_version if item_no == 1 else 1, "waiting_confirmation", item["question_text"], item["answer_text"], item_no)
                for item_id, item_no, item in zip(ids, range(1, len(values) + 1), values)
            ]
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
            cursor.execute("SELECT file_id,input_version,status,question_text,answer_text,item_no FROM intake_items WHERE id=%s AND user_id=%s", (intake_id, user_id))
            row = cursor.fetchone()
            connection.commit()
            return IntakeItem(intake_id, user_id, str(row[0]), int(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5]))
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
                "SELECT c.attempt_id,c.input_version,c.verdict,c.first_error,c.status,a.question_text,a.answer_text,c.evidence_text,a.question_id "
                "FROM grade_candidates c JOIN attempts a ON a.id=c.attempt_id "
                "WHERE c.id=%s AND c.user_id=%s AND a.user_id=%s FOR UPDATE",
                (candidate_id, user_id, user_id),
            )
            row = cursor.fetchone()
            if not row:
                raise LookupError("grade candidate not found")
            if int(row[1]) != expected_version:
                raise RuntimeError("input_version_changed")
            if str(row[2]) not in {"partial", "incorrect"}:
                raise RuntimeError("failed_final")
            validation = reference_validation_from_evidence(row[7])
            if validation and validation.get("status") == "conflict" and not reference_conflict_resolved(row[7]):
                raise RuntimeError("reference_conflict")
            attempt_id = str(row[0])
            cursor.execute("SELECT id,question_text,answer_text,first_error,status,created_at,question_id FROM error_notebook_entries WHERE user_id=%s AND attempt_id=%s FOR UPDATE", (user_id, attempt_id))
            existing = cursor.fetchone()
            if existing:
                self._ensure_review_tx(cursor, user_id, str(existing[0]), now)
                connection.commit()
                cursor.execute("SELECT evidence_text FROM grade_candidates WHERE id=%s AND user_id=%s", (candidate_id, user_id))
                evidence = (cursor.fetchone() or (None,))[0]
                return ErrorEntry(str(existing[0]), user_id, attempt_id, str(existing[1]), str(existing[2]), existing[3], str(existing[4]), existing[5].replace(tzinfo=timezone.utc), evidence, str(existing[6]) if existing[6] else None)
            error_id = uuid.uuid4().hex
            cursor.execute(
                "INSERT INTO error_notebook_entries (id,user_id,attempt_id,grade_candidate_id,question_id,question_text,answer_text,first_error,status,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,%s)",
                (error_id, user_id, attempt_id, candidate_id, row[8], str(row[5]), str(row[6]), row[3], now, now),
            )
            cursor.execute("UPDATE grade_candidates SET status='committed' WHERE id=%s AND user_id=%s", (candidate_id, user_id))
            cursor.execute("UPDATE attempts SET status='committed',updated_at=%s WHERE id=%s AND user_id=%s", (now, attempt_id, user_id))
            self._ensure_review_tx(cursor, user_id, error_id, now)
            cursor.execute("INSERT INTO domain_audit_events (user_id,event_type,resource_type,resource_id,metadata_json,occurred_at) VALUES (%s,'grade.committed','error',%s,%s,%s)", (user_id, error_id, json.dumps({"candidate_id": candidate_id, "question_id": row[8], "reference_status": validation.get("status") if validation else "not_found"}), now))
            connection.commit()
            return ErrorEntry(error_id, user_id, attempt_id, str(row[5]), str(row[6]), row[3], "open", now.replace(tzinfo=timezone.utc), row[7], str(row[8]) if row[8] else None)
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

    def find_reference_conflict_candidate(self, *, user_id: str, question_text: str) -> GradeCandidate | None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT c.id,c.attempt_id,c.input_version,c.verdict,c.first_error,c.evidence_text,c.status,a.question_text "
                "FROM grade_candidates c JOIN attempts a ON a.id=c.attempt_id AND a.user_id=c.user_id "
                "LEFT JOIN error_notebook_entries e ON e.user_id=c.user_id AND e.attempt_id=c.attempt_id "
                "WHERE c.user_id=%s AND c.status='candidate' AND e.id IS NULL ORDER BY c.created_at DESC LIMIT 50",
                (user_id,),
            )
            for row in cursor.fetchall():
                validation = reference_validation_from_evidence(row[5])
                if (
                    validation is not None
                    and validation.get("status") == "conflict"
                    and not reference_conflict_resolved(row[5])
                    and (str(row[7]) == question_text or question_match_score(str(row[7]), question_text) >= 0.92)
                ):
                    return GradeCandidate(str(row[0]), str(row[1]), int(row[2]), str(row[3]), row[4], row[5], str(row[6]))
            return None
        finally:
            cursor.close()
            connection.close()

    def list_errors(self, *, user_id: str) -> list[ErrorEntry]:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT e.id,e.attempt_id,e.question_text,e.answer_text,e.first_error,e.status,e.created_at,c.evidence_text,e.question_id FROM error_notebook_entries e JOIN grade_candidates c ON c.id=e.grade_candidate_id AND c.user_id=e.user_id WHERE e.user_id=%s AND e.status<>'removed' ORDER BY e.created_at DESC", (user_id,))
            return [ErrorEntry(str(row[0]), user_id, str(row[1]), str(row[2]), str(row[3]), row[4], str(row[5]), row[6].replace(tzinfo=timezone.utc), row[7], str(row[8]) if row[8] else None) for row in cursor.fetchall()]
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
            cursor.execute("SELECT e.attempt_id,e.question_text,e.answer_text,e.first_error,e.status,e.created_at,c.evidence_text,e.question_id FROM error_notebook_entries e JOIN grade_candidates c ON c.id=e.grade_candidate_id AND c.user_id=e.user_id WHERE e.id=%s AND e.user_id=%s", (error_id, user_id))
            row = cursor.fetchone()
            return ErrorEntry(error_id, user_id, str(row[0]), str(row[1]), str(row[2]), row[3], str(row[4]), row[5].replace(tzinfo=timezone.utc), row[6], str(row[7]) if row[7] else None) if row else None
        finally:
            cursor.close()
            connection.close()

    def set_error_status(self, *, user_id: str, error_id: str, status: str) -> ErrorEntry:
        if status not in {"mastered", "removed"}:
            raise ValueError("unsupported error status")
        connection = self._connect()
        cursor = connection.cursor()
        now = _utcnow()
        try:
            connection.begin()
            cursor.execute("SELECT id FROM error_notebook_entries WHERE id=%s AND user_id=%s AND status<>'removed' FOR UPDATE", (error_id, user_id))
            if not cursor.fetchone():
                raise LookupError("error not found")
            cursor.execute("UPDATE error_notebook_entries SET status=%s,updated_at=%s WHERE id=%s AND user_id=%s", (status, now, error_id, user_id))
            cursor.execute("UPDATE review_tasks SET status='cancelled' WHERE user_id=%s AND error_id=%s AND status IN ('pending','ready')", (user_id, error_id))
            if status == "removed":
                cursor.execute("UPDATE recommendations SET status='withdrawn' WHERE user_id=%s AND error_id=%s AND status IN ('candidate','assigned')", (user_id, error_id))
            cursor.execute("INSERT INTO domain_audit_events (user_id,event_type,resource_type,resource_id,metadata_json,occurred_at) VALUES (%s,%s,'error',%s,%s,%s)", (user_id, f"error.{status}", error_id, json.dumps({"status": status}), now))
            connection.commit()
            entry = self.get_error(user_id=user_id, error_id=error_id)
            if not entry and status == "removed":
                return ErrorEntry(error_id, user_id, "", "", "", None, status, now.replace(tzinfo=timezone.utc))
            if not entry:
                raise LookupError("error not found")
            return entry
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def assign_recommendations(self, *, user_id: str, error_id: str, limit: int = 2) -> tuple[list[Recommendation], bool]:
        error = self.get_error(user_id=user_id, error_id=error_id)
        if not error:
            raise LookupError("error not found")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT q.id,v.stem_text,v.answer_text,q.grade,q.difficulty,s.title "
                "FROM questions q JOIN question_sources s ON s.id=q.source_id "
                "JOIN question_versions v ON v.question_id=q.id AND v.version_no=q.current_version_no "
                "WHERE q.status='verified' AND s.license_status IN ('open','user_authorized') "
                "AND EXISTS (SELECT 1 FROM question_verifications x WHERE x.question_version_id=v.id AND x.verdict='verified') "
                "AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.user_id=%s AND a.question_id=q.id) "
                "AND NOT EXISTS (SELECT 1 FROM recommendations r WHERE r.user_id=%s AND r.error_id=%s AND r.question_id=q.id) LIMIT 200",
                (user_id, user_id, error_id),
            )
            candidates = [Question(str(row[0]), str(row[1]), row[2], int(row[3]) if row[3] is not None else None, float(row[4]) if row[4] is not None else None, str(row[5])) for row in cursor.fetchall()]
        finally:
            cursor.close()
            connection.close()
        ranked = rank_questions(error.question_text, candidates, limit)
        if ranked:
            now = _utcnow()
            day = learning_day()
            connection = self._connect()
            cursor = connection.cursor()
            try:
                connection.begin()
                cursor.execute("SELECT id FROM web_users WHERE id=%s FOR UPDATE", (user_id,))
                if not cursor.fetchone():
                    raise LookupError("user not found")
                cursor.execute("SELECT COUNT(*) FROM daily_learning_usage WHERE user_id=%s AND usage_date=%s AND kind='recommendation' AND status='counted'", (user_id, day))
                used = int((cursor.fetchone() or (0,))[0])
                if used >= DAILY_RECOMMENDATION_LIMIT:
                    connection.commit()
                    items = self.list_recommendations(user_id=user_id, error_id=error_id)
                    return items[:limit], True
                for question, reason in ranked[: DAILY_RECOMMENDATION_LIMIT - used]:
                    recommendation_id = uuid.uuid4().hex
                    resource_id = hashlib.sha256(f"{error_id}:{question.question_id}".encode("ascii")).hexdigest()
                    cursor.execute(
                        "INSERT INTO recommendations (id,user_id,error_id,question_id,reason,status,created_at) "
                        "VALUES (%s,%s,%s,%s,%s,'assigned',%s)",
                        (recommendation_id, user_id, error_id, question.question_id, reason, now),
                    )
                    cursor.execute(
                        "INSERT INTO daily_learning_usage (user_id,usage_date,kind,resource_id,status,created_at,updated_at) VALUES (%s,%s,'recommendation',%s,'counted',%s,%s)",
                        (user_id, day, resource_id, now, now),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                cursor.close()
                connection.close()
        items = self.list_recommendations(user_id=user_id, error_id=error_id)
        return items[:limit], len(items) < limit

    def list_recommendations(self, *, user_id: str, error_id: str) -> list[Recommendation]:
        if not self.get_error(user_id=user_id, error_id=error_id):
            raise LookupError("error not found")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT r.id,r.reason,r.status,q.id,v.stem_text,v.answer_text,q.grade,q.difficulty,s.title "
                "FROM recommendations r JOIN questions q ON q.id=r.question_id "
                "JOIN question_sources s ON s.id=q.source_id "
                "JOIN question_versions v ON v.question_id=q.id AND v.version_no=q.current_version_no "
                "WHERE r.user_id=%s AND r.error_id=%s AND r.status IN ('assigned','completed') "
                "AND q.status='verified' AND s.license_status IN ('open','user_authorized') "
                "AND EXISTS (SELECT 1 FROM question_verifications x WHERE x.question_version_id=v.id AND x.verdict='verified') "
                "ORDER BY r.created_at,r.id",
                (user_id, error_id),
            )
            return [Recommendation(str(row[0]), user_id, error_id, Question(str(row[3]), str(row[4]), row[5], int(row[6]) if row[6] is not None else None, float(row[7]) if row[7] is not None else None, str(row[8])), str(row[1]), str(row[2])) for row in cursor.fetchall()]
        finally:
            cursor.close()
            connection.close()

    def list_due_reviews(self, *, user_id: str, now: datetime | None = None) -> list[ReviewTask]:
        current = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT id,error_id,stage,due_at FROM review_tasks WHERE user_id=%s AND status IN ('pending','ready') AND due_at<=%s ORDER BY due_at,error_id", (user_id, current))
            rows = cursor.fetchall()
            return [ReviewTask(str(row[0]), user_id, str(row[1]), int(row[2]), row[3].replace(tzinfo=timezone.utc), "ready") for row in rows]
        finally:
            cursor.close()
            connection.close()

    def list_active_reviews(self, *, user_id: str) -> list[ReviewTask]:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT id,error_id,stage,due_at,status FROM review_tasks WHERE user_id=%s AND status IN ('pending','ready') ORDER BY due_at,error_id", (user_id,))
            return [ReviewTask(str(row[0]), user_id, str(row[1]), int(row[2]), row[3].replace(tzinfo=timezone.utc), str(row[4])) for row in cursor.fetchall()]
        finally:
            cursor.close()
            connection.close()

    def complete_review(self, *, user_id: str, task_id: str, result: str, idempotency_key: str, now: datetime | None = None) -> ReviewTask | None:
        key = _required(idempotency_key, "idempotency_key")
        completed_at = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute("SELECT error_id FROM review_attempts WHERE user_id=%s AND idempotency_key=%s", (user_id, key))
            existing = cursor.fetchone()
            if existing:
                next_task = self._active_review_tx(cursor, user_id, str(existing[0]))
                connection.commit()
                return next_task
            cursor.execute("SELECT error_id,stage,due_at,status FROM review_tasks WHERE id=%s AND user_id=%s FOR UPDATE", (task_id, user_id))
            row = cursor.fetchone()
            if not row:
                raise LookupError("review task not found")
            if str(row[3]) not in {"pending", "ready"} or row[2] > completed_at:
                raise RuntimeError("conflict")
            error_id, stage = str(row[0]), int(row[1])
            target = next_review(stage, result, completed_at.replace(tzinfo=timezone.utc))
            cursor.execute(
                "INSERT INTO review_attempts (id,user_id,review_task_id,error_id,stage,result,idempotency_key,completed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (uuid.uuid4().hex, user_id, task_id, error_id, stage, result, key, completed_at),
            )
            cursor.execute("UPDATE review_tasks SET status='completed' WHERE id=%s AND user_id=%s", (task_id, user_id))
            cursor.execute("UPDATE review_tasks SET status='cancelled' WHERE user_id=%s AND error_id=%s AND id<>%s AND status IN ('pending','ready')", (user_id, error_id, task_id))
            next_task = None
            if target is None:
                cursor.execute("UPDATE error_notebook_entries SET status='mastered',updated_at=%s WHERE id=%s AND user_id=%s", (completed_at, error_id, user_id))
            else:
                target_stage, target_due = target
                target_due = target_due.replace(tzinfo=None)
                next_id = uuid.uuid4().hex
                cursor.execute(
                    "INSERT INTO review_tasks (id,user_id,error_id,stage,due_at,status,created_at) VALUES (%s,%s,%s,%s,%s,'pending',%s) "
                    "ON DUPLICATE KEY UPDATE due_at=VALUES(due_at),status='pending'",
                    (next_id, user_id, error_id, target_stage, target_due, completed_at),
                )
                cursor.execute("SELECT id,error_id,stage,due_at,status FROM review_tasks WHERE user_id=%s AND error_id=%s AND stage=%s", (user_id, error_id, target_stage))
                next_row = cursor.fetchone()
                next_task = ReviewTask(str(next_row[0]), user_id, str(next_row[1]), int(next_row[2]), next_row[3].replace(tzinfo=timezone.utc), str(next_row[4]))
            cursor.execute("INSERT INTO domain_audit_events (user_id,event_type,resource_type,resource_id,metadata_json,occurred_at) VALUES (%s,'review.completed','error',%s,%s,%s)", (user_id, error_id, json.dumps({"stage": stage, "result": result}), completed_at))
            connection.commit()
            return next_task
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def progress(self, *, user_id: str, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
        today_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today_start + timedelta(days=1)
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT COUNT(*),SUM(status='mastered') FROM error_notebook_entries WHERE user_id=%s AND status<>'removed'", (user_id,))
            totals = cursor.fetchone() or (0, 0)
            cursor.execute("SELECT COUNT(*) FROM review_tasks WHERE user_id=%s AND status IN ('pending','ready') AND due_at<=%s", (user_id, current))
            due = cursor.fetchone() or (0,)
            cursor.execute("SELECT COUNT(*) FROM error_notebook_entries e WHERE e.user_id=%s AND e.status<>'removed' AND NOT EXISTS (SELECT 1 FROM recommendations r WHERE r.user_id=e.user_id AND r.error_id=e.id AND r.status='assigned')", (user_id,))
            gaps = cursor.fetchone() or (0,)
            cursor.execute("SELECT COUNT(*),SUM(result='correct'),SUM(result='partial'),SUM(result='wrong') FROM review_attempts WHERE user_id=%s", (user_id,))
            reviews = cursor.fetchone() or (0, 0, 0, 0)
            cursor.execute("SELECT stage,COUNT(*) FROM review_tasks WHERE user_id=%s AND status IN ('pending','ready') GROUP BY stage", (user_id,))
            stage_counts = {str(stage): 0 for stage in range(1, 7)}
            for stage, total in cursor.fetchall():
                stage_counts[str(int(stage))] = int(total)
            cursor.execute("SELECT COUNT(*),SUM(result IN ('partial','wrong')) FROM review_attempts WHERE user_id=%s AND completed_at>=%s AND completed_at<%s", (user_id, today_start, tomorrow))
            today_reviews = cursor.fetchone() or (0, 0)
            count = int(totals[0] or 0)
            completed = int(reviews[0] or 0)
            correct = int(reviews[1] or 0)
            return {"error_count": count, "mastered_count": int(totals[1] or 0), "due_review_count": int(due[0]), "recommendation_gap_count": int(gaps[0]), "completed_review_count": completed, "correct_review_count": correct, "partial_review_count": int(reviews[2] or 0), "wrong_review_count": int(reviews[3] or 0), "review_accuracy_percent": round(correct * 100 / completed) if completed else 0, "review_stage_counts": stage_counts, "today_completed_review_count": int(today_reviews[0] or 0), "today_needs_correction_count": int(today_reviews[1] or 0), "sample_sufficient": count >= 3}
        finally:
            cursor.close()
            connection.close()

    def review_calendar(self, *, user_id: str, month: str, now: datetime | None = None) -> dict[str, object]:
        start, end = calendar_month_range(month)
        start_db, end_db = start.replace(tzinfo=None), end.replace(tzinfo=None)
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM error_notebook_entries WHERE user_id=%s AND status<>'removed'", (user_id,))
            total_error_count = int((cursor.fetchone() or (0,))[0])
            cursor.execute(
                "SELECT e.id,e.question_text,e.first_error,c.evidence_text,e.created_at,e.status "
                "FROM error_notebook_entries e JOIN grade_candidates c ON c.id=e.grade_candidate_id AND c.user_id=e.user_id "
                "WHERE e.user_id=%s AND e.status<>'removed' AND e.created_at>=%s AND e.created_at<%s ORDER BY e.created_at,e.id",
                (user_id, start_db, end_db),
            )
            errors = [
                {"error_id": str(row[0]), "question_text": str(row[1]), "first_error": row[2], "evidence": row[3], "created_at": row[4], "status": str(row[5])}
                for row in cursor.fetchall()
            ]
            cursor.execute(
                "SELECT t.error_id,e.question_text,e.first_error,c.evidence_text,t.stage,t.due_at,t.status "
                "FROM review_tasks t JOIN error_notebook_entries e ON e.id=t.error_id AND e.user_id=t.user_id "
                "JOIN grade_candidates c ON c.id=e.grade_candidate_id AND c.user_id=e.user_id "
                "WHERE t.user_id=%s AND e.status<>'removed' AND t.status<>'cancelled' AND t.due_at>=%s AND t.due_at<%s ORDER BY t.due_at,t.id",
                (user_id, start_db, end_db),
            )
            tasks = [
                {"error_id": str(row[0]), "question_text": str(row[1]), "first_error": row[2], "evidence": row[3], "stage": int(row[4]), "due_at": row[5], "status": str(row[6])}
                for row in cursor.fetchall()
            ]
            cursor.execute(
                "SELECT a.error_id,e.question_text,e.first_error,c.evidence_text,a.stage,a.result,a.completed_at "
                "FROM review_attempts a JOIN error_notebook_entries e ON e.id=a.error_id AND e.user_id=a.user_id "
                "JOIN grade_candidates c ON c.id=e.grade_candidate_id AND c.user_id=e.user_id "
                "WHERE a.user_id=%s AND e.status<>'removed' AND a.completed_at>=%s AND a.completed_at<%s ORDER BY a.completed_at,a.id",
                (user_id, start_db, end_db),
            )
            attempts = [
                {"error_id": str(row[0]), "question_text": str(row[1]), "first_error": row[2], "evidence": row[3], "stage": int(row[4]), "result": str(row[5]), "completed_at": row[6], "status": "completed"}
                for row in cursor.fetchall()
            ]
            return build_review_calendar(month, errors=errors, review_tasks=tasks, review_attempts=attempts, total_error_count=total_error_count, now=now)
        finally:
            cursor.close()
            connection.close()

    def bank_status(self) -> dict[str, int]:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT COUNT(*),SUM(q.status='verified' AND s.license_status IN ('open','user_authorized')),SUM(q.status='candidate') FROM questions q JOIN question_sources s ON s.id=q.source_id")
            row = cursor.fetchone() or (0, 0, 0)
            return {"question_count": int(row[0] or 0), "recommendable_count": int(row[1] or 0), "candidate_count": int(row[2] or 0)}
        finally:
            cursor.close()
            connection.close()

    def pending_job_count(self, *, user_id: str) -> int:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM web_jobs WHERE user_id=%s AND status NOT IN ('completed','cancelled','failed_final')", (user_id,))
            return int((cursor.fetchone() or (0,))[0])
        finally:
            cursor.close()
            connection.close()

    def practice_items(self, *, user_id: str, error_ids: list[str]) -> tuple[list[dict[str, Any]], int]:
        items: list[dict[str, Any]] = []
        seen_questions: set[str] = set()
        gaps = 0
        placeholders = ",".join(["%s"] * len(error_ids))
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                f"SELECT e.id,COALESCE((SELECT t.stage FROM review_tasks t WHERE t.user_id=e.user_id AND t.error_id=e.id AND t.status IN ('pending','ready') ORDER BY t.due_at LIMIT 1),IF(e.status='mastered',6,1)),"
                f"(SELECT a.result FROM review_attempts a WHERE a.user_id=e.user_id AND a.error_id=e.id ORDER BY a.completed_at DESC,a.id DESC LIMIT 1) "
                f"FROM error_notebook_entries e WHERE e.user_id=%s AND e.id IN ({placeholders})",
                (user_id, *error_ids),
            )
            review_context = {str(row[0]): (int(row[1]), str(row[2]) if row[2] else None) for row in cursor.fetchall()}
        finally:
            cursor.close()
            connection.close()
        for error_id in error_ids:
            error = self.get_error(user_id=user_id, error_id=error_id)
            if not error:
                raise LookupError("error not found")
            stage, latest_result = review_context.get(error_id, (1, None))
            requires_original = review_requires_original(stage, latest_result)
            reason = "订正回退" if latest_result in {"partial", "wrong"} else f"第 {stage} 阶段"
            items.append({"kind": "original", "error_id": error_id, "question_id": None, "stem_text": error.question_text, "answer_text": None, "difficulty": None, "source_title": "个人错题本", "reason": reason, "review_stage": stage, "requires_original": requires_original})
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
        key = _required(idempotency_key, "idempotency_key")
        if not error_ids:
            raise ValueError("error_ids is required")
        connection = self._connect()
        cursor = connection.cursor()
        now = _utcnow()
        try:
            connection.begin()
            placeholders = ",".join(["%s"] * len(error_ids))
            cursor.execute(f"SELECT COUNT(*) FROM error_notebook_entries WHERE user_id=%s AND id IN ({placeholders}) AND status<>'removed'", (user_id, *error_ids))
            if int((cursor.fetchone() or (0,))[0]) != len(set(error_ids)):
                raise LookupError("error not found")
            digest = hashlib.sha256(json.dumps([sorted(set(error_ids)), include_answers], separators=(",", ":")).encode("ascii")).hexdigest()
            proposed = uuid.uuid4().hex
            cursor.execute(
                "INSERT INTO web_jobs (id,user_id,job_type,resource_type,resource_id,idempotency_key,input_sha256,status,checkpoint_json,created_at,updated_at) "
                "VALUES (%s,%s,'practice_pdf','error',%s,%s,%s,'queued',%s,%s,%s) ON DUPLICATE KEY UPDATE updated_at=updated_at",
                (proposed, user_id, error_ids[0], key, digest, json.dumps({"error_ids": error_ids, "include_answers": include_answers}), now, now),
            )
            cursor.execute("SELECT id,resource_id,status,checkpoint_json,last_error_code,input_sha256 FROM web_jobs WHERE user_id=%s AND job_type='practice_pdf' AND idempotency_key=%s", (user_id, key))
            row = cursor.fetchone()
            if str(row[5]) != digest:
                raise RuntimeError("conflict")
            connection.commit()
            return Job(str(row[0]), user_id, "practice_pdf", str(row[1]), str(row[2]), self._json(row[3]), row[4])
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def complete_practice_job(self, *, user_id: str, job_id: str, file_id: str, question_count: int, recommendation_gap_count: int, include_answers: bool) -> Job:
        checkpoint = {"file_id": file_id, "question_count": question_count, "recommendation_gap_count": recommendation_gap_count, "include_answers": include_answers}
        connection = self._connect()
        cursor = connection.cursor()
        now = _utcnow()
        try:
            connection.begin()
            changed = cursor.execute("UPDATE web_jobs SET status='completed',checkpoint_json=%s,result_json=%s,updated_at=%s WHERE id=%s AND user_id=%s AND job_type='practice_pdf'", (json.dumps(checkpoint), json.dumps(checkpoint), now, job_id, user_id))
            if changed != 1:
                raise LookupError("job not found")
            cursor.execute("SELECT resource_id FROM web_jobs WHERE id=%s AND user_id=%s", (job_id, user_id))
            row = cursor.fetchone()
            connection.commit()
            return Job(job_id, user_id, "practice_pdf", str(row[0]), "completed", checkpoint, None)
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def list_practice_pdfs(self, *, user_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT j.id,j.checkpoint_json,j.updated_at,f.original_name,f.byte_size "
                "FROM web_jobs j JOIN web_files f ON f.id=JSON_UNQUOTE(JSON_EXTRACT(j.checkpoint_json,'$.file_id')) AND f.user_id=j.user_id "
                "WHERE j.user_id=%s AND j.job_type='practice_pdf' AND j.status='completed' AND f.purpose='practice_pdf' AND f.status='ready' "
                "ORDER BY j.updated_at DESC",
                (user_id,),
            )
            items = []
            for job_id, checkpoint_json, updated_at, original_name, byte_size in cursor.fetchall():
                checkpoint = self._json(checkpoint_json) or {}
                filename = checkpoint.get("filename") if isinstance(checkpoint.get("filename"), str) else original_name
                items.append({
                    "task_id": str(job_id),
                    "filename": str(filename),
                    "byte_size": int(byte_size),
                    "generated_at": updated_at.replace(tzinfo=timezone.utc).isoformat(),
                    "question_count": int(checkpoint.get("question_count", 0)),
                    "include_answers": bool(checkpoint.get("include_answers", False)),
                    "source": str(checkpoint.get("source", "generated")),
                })
            return items
        finally:
            cursor.close()
            connection.close()

    def create_export_job(self, *, user_id: str, idempotency_key: str, expires_at: datetime) -> Job:
        key = _required(idempotency_key, "idempotency_key")
        now = _utcnow()
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute("SELECT id FROM web_users WHERE id=%s AND status='active' FOR UPDATE", (user_id,))
            if not cursor.fetchone():
                raise PermissionError("active account required")
            proposed = uuid.uuid4().hex
            digest = hashlib.sha256(b"personal-export-v1").hexdigest()
            cursor.execute(
                "INSERT INTO web_jobs (id,user_id,job_type,resource_type,resource_id,idempotency_key,input_sha256,status,checkpoint_json,created_at,updated_at) "
                "VALUES (%s,%s,'export','export',%s,%s,%s,'queued',%s,%s,%s) ON DUPLICATE KEY UPDATE updated_at=updated_at",
                (proposed, user_id, proposed, key, digest, json.dumps({"expires_at": expires_at.isoformat()}), now, now),
            )
            cursor.execute("SELECT id,resource_id,status,checkpoint_json,last_error_code FROM web_jobs WHERE user_id=%s AND job_type='export' AND idempotency_key=%s FOR UPDATE", (user_id, key))
            row = cursor.fetchone()
            if not row:
                raise RuntimeError("export job did not persist")
            connection.commit()
            return Job(str(row[0]), user_id, "export", str(row[1]), str(row[2]), self._json(row[3]), row[4])
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def complete_export_job(self, *, user_id: str, job_id: str, file_id: str, expires_at: datetime) -> Job:
        checkpoint = {"file_id": file_id, "expires_at": expires_at.isoformat()}
        connection = self._connect()
        cursor = connection.cursor()
        now = _utcnow()
        try:
            connection.begin()
            changed = cursor.execute("UPDATE web_jobs SET status='completed',checkpoint_json=%s,result_json=%s,updated_at=%s WHERE id=%s AND user_id=%s AND job_type='export'", (json.dumps(checkpoint), json.dumps(checkpoint), now, job_id, user_id))
            if changed != 1:
                raise LookupError("export not found")
            cursor.execute("SELECT resource_id FROM web_jobs WHERE id=%s AND user_id=%s", (job_id, user_id))
            row = cursor.fetchone()
            connection.commit()
            return Job(job_id, user_id, "export", str(row[0]), "completed", checkpoint, None)
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def claim_export_download(self, *, user_id: str, job_id: str, maximum: int) -> bool:
        connection = self._connect()
        cursor = connection.cursor()
        now = _utcnow()
        try:
            connection.begin()
            cursor.execute("SELECT id FROM web_jobs WHERE id=%s AND user_id=%s AND job_type='export' FOR UPDATE", (job_id, user_id))
            if not cursor.fetchone():
                raise LookupError("export not found")
            cursor.execute("SELECT COUNT(*) FROM domain_audit_events WHERE user_id=%s AND event_type='export.downloaded' AND resource_type='export' AND resource_id=%s", (user_id, job_id))
            row = cursor.fetchone()
            completed = int(row[0]) if row else 0
            allowed = completed < maximum
            event_type = "export.downloaded" if allowed else "export.download_denied"
            outcome = "allowed" if allowed else "download_limit"
            cursor.execute(
                "INSERT INTO domain_audit_events (user_id,event_type,resource_type,resource_id,metadata_json,occurred_at) VALUES (%s,%s,'export',%s,%s,%s)",
                (user_id, event_type, job_id, json.dumps({"outcome": outcome, "successful_downloads": completed}), now),
            )
            connection.commit()
            return allowed
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def export_data(self, *, user_id: str) -> dict[str, Any]:
        """Return the frozen user business export; never auth, audit, hashes or object keys."""
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT id,purpose,original_name,media_type,byte_size,status,created_at,updated_at FROM web_files WHERE user_id=%s ORDER BY created_at,id", (user_id,))
            files = [dict(zip(("file_id", "purpose", "original_name", "media_type", "byte_size", "status", "created_at", "updated_at"), row)) for row in cursor.fetchall()]
            cursor.execute("SELECT id,file_id,item_no,input_version,status,question_text,answer_text,created_at,updated_at FROM intake_items WHERE user_id=%s ORDER BY created_at,id", (user_id,))
            intakes = [dict(zip(("intake_id", "file_id", "item_no", "input_version", "status", "question_text", "answer_text", "created_at", "updated_at"), row)) for row in cursor.fetchall()]
            cursor.execute("SELECT id,attempt_id,input_version,verdict,first_error,evidence_text,confidence,status,created_at FROM grade_candidates WHERE user_id=%s ORDER BY created_at,id", (user_id,))
            grade_candidates = [dict(zip(("candidate_id", "attempt_id", "input_version", "verdict", "first_error", "evidence", "confidence", "status", "created_at"), row)) for row in cursor.fetchall()]
            cursor.execute("SELECT id,attempt_id,question_text,answer_text,first_error,status,created_at,updated_at FROM error_notebook_entries WHERE user_id=%s ORDER BY created_at,id", (user_id,))
            errors = [dict(zip(("error_id", "attempt_id", "question_text", "answer_text", "first_error", "status", "created_at", "updated_at"), row)) for row in cursor.fetchall()]
            cursor.execute("SELECT id,intake_id,input_version,question_text,answer_text,status,created_at,updated_at FROM attempts WHERE user_id=%s ORDER BY created_at,id", (user_id,))
            attempts = [dict(zip(("attempt_id", "intake_id", "input_version", "question_text", "answer_text", "status", "created_at", "updated_at"), row)) for row in cursor.fetchall()]
            cursor.execute("SELECT id,error_id,question_id,reason,status,created_at FROM recommendations WHERE user_id=%s ORDER BY created_at,id", (user_id,))
            recommendations = [dict(zip(("recommendation_id", "error_id", "question_id", "reason", "status", "created_at"), row)) for row in cursor.fetchall()]
            cursor.execute("SELECT usage_date,kind,resource_id,status,created_at FROM daily_learning_usage WHERE user_id=%s ORDER BY usage_date,kind,resource_id", (user_id,))
            learning_usage = [
                {"date": row[0].isoformat(), "kind": str(row[1]), "resource_id": str(row[2]), "status": str(row[3]), "created_at": row[4]}
                for row in cursor.fetchall()
            ]
            cursor.execute("SELECT id,error_id,stage,due_at,status,created_at FROM review_tasks WHERE user_id=%s ORDER BY created_at,id", (user_id,))
            reviews = [dict(zip(("review_id", "error_id", "stage", "due_at", "status", "created_at"), row)) for row in cursor.fetchall()]
            cursor.execute("SELECT id,review_task_id,error_id,stage,result,completed_at FROM review_attempts WHERE user_id=%s ORDER BY completed_at,id", (user_id,))
            review_attempts = [dict(zip(("review_attempt_id", "review_id", "error_id", "stage", "result", "completed_at"), row)) for row in cursor.fetchall()]
            cursor.execute("SELECT id,job_type,resource_type,resource_id,status,created_at,updated_at FROM web_jobs WHERE user_id=%s AND job_type<>'export' ORDER BY created_at,id", (user_id,))
            jobs = [dict(zip(("job_id", "job_type", "resource_type", "resource_id", "status", "created_at", "updated_at"), row)) for row in cursor.fetchall()]
            return {"schema_version": 2, "files": files, "intakes": intakes, "attempts": attempts, "grade_candidates": grade_candidates, "errors": errors, "recommendations": recommendations, "learning_usage": learning_usage, "review_tasks": reviews, "review_attempts": review_attempts, "jobs": jobs}
        finally:
            cursor.close()
            connection.close()

    def begin_user_deletion(self, *, user_id: str) -> dict[str, Any]:
        """Durably record pending before any cross-service account mutation."""
        now = _utcnow()
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute(
                "INSERT INTO account_deletions (user_id,requested_at,status,updated_at,last_error_code) VALUES (%s,%s,'pending',%s,NULL) "
                "ON DUPLICATE KEY UPDATE updated_at=updated_at",
                (user_id, now, now),
            )
            cursor.execute("SELECT status,updated_at,last_error_code FROM account_deletions WHERE user_id=%s FOR UPDATE", (user_id,))
            row = cursor.fetchone()
            if not row:
                raise RuntimeError("deletion marker did not persist")
            connection.commit()
            return {"status": str(row[0]), "updated_at": row[1], "last_error_code": row[2]}
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def deletion_status(self, *, user_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT status,updated_at,last_error_code FROM account_deletions WHERE user_id=%s", (user_id,))
            row = cursor.fetchone()
            return {"status": str(row[0]), "updated_at": row[1], "last_error_code": row[2]} if row else None
        finally:
            cursor.close()
            connection.close()

    def deactivate_user_data(self, *, user_id: str) -> None:
        """Idempotently make retained business rows inaccessible after the durable pending marker."""
        self.begin_user_deletion(user_id=user_id)
        now = _utcnow()
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute("UPDATE web_jobs SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,updated_at=%s WHERE user_id=%s AND status IN ('queued','running','waiting_confirmation','failed_retryable')", (now, user_id))
            cursor.execute("UPDATE web_files SET status='deleted',updated_at=%s WHERE user_id=%s AND status<>'deleted'", (now, user_id))
            cursor.execute("UPDATE intake_items SET status='cancelled',updated_at=%s WHERE user_id=%s AND status IN ('extracting','waiting_confirmation','confirmed')", (now, user_id))
            cursor.execute("UPDATE attempts SET status='cancelled',updated_at=%s WHERE user_id=%s AND status IN ('grading','grade_ready')", (now, user_id))
            cursor.execute("UPDATE review_tasks SET status='cancelled' WHERE user_id=%s AND status IN ('pending','ready')", (user_id,))
            cursor.execute("UPDATE recommendations SET status='withdrawn' WHERE user_id=%s AND status IN ('candidate','assigned','completed')", (user_id,))
            cursor.execute("UPDATE error_notebook_entries SET status='removed',updated_at=%s WHERE user_id=%s AND status<>'removed'", (now, user_id))
            connection.commit()
        except Exception:
            connection.rollback()
            self._deletion_error(user_id=user_id, code="domain_cleanup_failed")
            raise
        finally:
            cursor.close()
            connection.close()

    def deletion_file_keys(self, *, user_id: str) -> list[str]:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT object_key FROM web_files WHERE user_id=%s AND status='deleted'", (user_id,))
            return [str(row[0]) for row in cursor.fetchall()]
        finally:
            cursor.close()
            connection.close()

    def pending_deletion_user_ids(self) -> list[str]:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT user_id FROM account_deletions WHERE status='pending' ORDER BY user_id")
            return [str(row[0]) for row in cursor.fetchall()]
        finally:
            cursor.close()
            connection.close()

    def complete_user_deletion(self, *, user_id: str) -> dict[str, Any]:
        now = _utcnow()
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            changed = cursor.execute("UPDATE account_deletions SET status='completed',updated_at=%s,last_error_code=NULL WHERE user_id=%s AND status='pending'", (now, user_id))
            if changed != 1:
                cursor.execute("SELECT status,updated_at,last_error_code FROM account_deletions WHERE user_id=%s FOR UPDATE", (user_id,))
                row = cursor.fetchone()
                if not row:
                    raise LookupError("deletion not found")
                connection.commit()
                return {"status": str(row[0]), "updated_at": row[1], "last_error_code": row[2]}
            cursor.execute("SELECT status,updated_at,last_error_code FROM account_deletions WHERE user_id=%s FOR UPDATE", (user_id,))
            row = cursor.fetchone()
            connection.commit()
            return {"status": str(row[0]), "updated_at": row[1], "last_error_code": row[2]}
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def record_deletion_error(self, *, user_id: str, code: str) -> None:
        self._deletion_error(user_id=user_id, code=code)

    def _deletion_error(self, *, user_id: str, code: str) -> None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute("UPDATE account_deletions SET status='pending',updated_at=%s,last_error_code=%s WHERE user_id=%s AND status='pending'", (_utcnow(), code[:64], user_id))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    @staticmethod
    def _ensure_review_tx(cursor: Cursor, user_id: str, error_id: str, now: datetime) -> None:
        cursor.execute(
            "INSERT INTO review_tasks (id,user_id,error_id,stage,due_at,status,created_at) VALUES (%s,%s,%s,1,%s,'ready',%s) "
            "ON DUPLICATE KEY UPDATE id=id",
            (uuid.uuid4().hex, user_id, error_id, now, now),
        )

    @staticmethod
    def _active_review_tx(cursor: Cursor, user_id: str, error_id: str) -> ReviewTask | None:
        cursor.execute("SELECT id,error_id,stage,due_at,status FROM review_tasks WHERE user_id=%s AND error_id=%s AND status IN ('pending','ready') ORDER BY due_at LIMIT 1", (user_id, error_id))
        row = cursor.fetchone()
        return ReviewTask(str(row[0]), user_id, str(row[1]), int(row[2]), row[3].replace(tzinfo=timezone.utc), str(row[4])) if row else None

    @staticmethod
    def _json(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        return json.loads(str(value))
