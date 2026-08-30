"""MySQL 8 adapter for fenced asynchronous intake batches."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Callable
import uuid

from .intake_batch import (
    BatchClaim,
    BatchOperation,
    IDEMPOTENCY_KEY,
    IntakeBatch,
    IntakeBatchEvent,
    IntakeBatchFile,
    NONTERMINAL_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    parsed = json.loads(value) if value else {}
    return parsed if isinstance(parsed, dict) else {}


class MySqlIntakeBatchRepository:
    """All claims, operation receipts, progress and events use one fenced transaction."""

    def __init__(self, connection_factory, *, max_files: int = 8) -> None:
        self._connect = connection_factory
        self._max_files = max_files

    def create_batch(self, *, user_id: str, file_ids: list[str], idempotency_key: str) -> tuple[IntakeBatch, bool]:
        self._validate_create(file_ids, idempotency_key)
        fingerprint = hashlib.sha256(json.dumps(
            {"schema": "intake-batch-create/v1", "file_ids": file_ids},
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")).hexdigest()
        proposed = uuid.uuid4().hex
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute("SELECT id FROM web_users WHERE id=%s FOR UPDATE", (user_id,))
            if not cursor.fetchone():
                raise LookupError("batch input file not found")
            now = self._db_now_tx(cursor)
            cursor.execute(
                "SELECT id,request_sha256 FROM intake_batches "
                "WHERE user_id=%s AND operation_version='intake-batch-create/v1' AND idempotency_key=%s FOR UPDATE",
                (user_id, idempotency_key),
            )
            stored = cursor.fetchone()
            if stored:
                batch_id, stored_fingerprint = str(stored[0]), str(stored[1])
                if stored_fingerprint != fingerprint:
                    raise RuntimeError("idempotency_conflict")
                record = self._load_batch_tx(cursor, batch_id=batch_id, user_id=user_id, lock=True)
                if record is None:
                    raise RuntimeError("batch did not persist")
                connection.commit()
                return self._public(record), False
            cursor.execute("SELECT status FROM account_deletions WHERE user_id=%s FOR UPDATE", (user_id,))
            if cursor.fetchone():
                raise RuntimeError("account_deleted")
            cursor.execute(
                "SELECT COUNT(*) FROM intake_batches WHERE user_id=%s "
                "AND status IN ('pending','slicing','solving','grading')",
                (user_id,),
            )
            if int(cursor.fetchone()[0]) >= 3:
                raise RuntimeError("batch_limit_reached")
            cursor.execute(
                "SELECT COUNT(*) FROM intake_batches WHERE user_id=%s "
                "AND created_at>=DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 24 HOUR)",
                (user_id,),
            )
            if int(cursor.fetchone()[0]) >= 60:
                raise RuntimeError("batch_rate_limited")
            batch_id = proposed
            cursor.execute(
                "INSERT INTO intake_batches "
                "(id,user_id,operation_version,idempotency_key,request_sha256,status,total_files,retry_at,created_at,updated_at) "
                "VALUES (%s,%s,'intake-batch-create/v1',%s,%s,'pending',%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))",
                (batch_id, user_id, idempotency_key, fingerprint, len(file_ids)),
            )
            created = True
            if created:
                placeholders = ",".join(["%s"] * len(file_ids))
                cursor.execute(
                    "SELECT id,purpose,object_key,content_sha256,media_type,byte_size,status "
                    f"FROM web_files WHERE user_id=%s AND id IN ({placeholders}) FOR UPDATE",
                    (user_id, *file_ids),
                )
                records = {str(row[0]): row for row in cursor.fetchall()}
                if set(records) != set(file_ids):
                    raise LookupError("batch input file not found")
                for ordinal, file_id in enumerate(file_ids, 1):
                    row = records[file_id]
                    if str(row[1]) != "question_image" or str(row[4]) not in {"image/png", "image/jpeg"} or str(row[6]) != "ready":
                        raise LookupError("batch input file not found")
                    cursor.execute(
                        "INSERT INTO intake_batch_files "
                        "(batch_id,file_ordinal,file_id,object_key,content_sha256,media_type,byte_size,created_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (batch_id, ordinal, file_id, str(row[2]), str(row[3]), str(row[4]), int(row[5]), now),
                    )
                record = self._load_batch_tx(cursor, batch_id=batch_id, user_id=user_id, lock=True)
                assert record is not None
                self._append_event_tx(cursor, record, "progress", now)
            record = self._load_batch_tx(cursor, batch_id=batch_id, user_id=user_id, lock=True)
            if record is None:
                raise RuntimeError("batch did not persist")
            connection.commit()
            return self._public(record), created
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def get_batch(self, *, user_id: str, batch_id: str) -> IntakeBatch | None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            record = self._load_batch_tx(cursor, batch_id=batch_id, user_id=user_id, lock=False)
            return self._public(record) if record else None
        finally:
            cursor.close()
            connection.close()

    def find_active_batch(self, *, user_id: str) -> IntakeBatch | None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT id,user_id,status,total_files,completed_files,total_items,completed_items,stage_completed_items,"
                "last_event_id,error_code,slicing_attempts,solving_attempts,grading_attempts,retry_at,slot_id,claim_epoch,"
                "lease_owner,lease_expires_at,created_at,updated_at FROM intake_batches "
                "WHERE user_id=%s AND status IN ('pending','slicing','solving','grading') "
                "ORDER BY updated_at DESC,id DESC LIMIT 1",
                (user_id,),
            )
            row = cursor.fetchone()
            return self._public(self._row(row)) if row else None
        finally:
            cursor.close()
            connection.close()

    def recovery_cursor(self) -> datetime:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT UTC_TIMESTAMP(6)")
            row = cursor.fetchone()
            if not row:
                raise RuntimeError("database clock unavailable")
            return _aware(row[0])
        finally:
            cursor.close()
            connection.close()

    def find_recoverable_batch(self, *, user_id: str, updated_after: datetime) -> IntakeBatch | None:
        if not isinstance(updated_after, datetime) or updated_after.tzinfo is None:
            raise ValueError("invalid recovery cursor")
        threshold = updated_after.astimezone(timezone.utc).replace(tzinfo=None)
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT id,user_id,status,total_files,completed_files,total_items,completed_items,stage_completed_items,"
                "last_event_id,error_code,slicing_attempts,solving_attempts,grading_attempts,retry_at,slot_id,claim_epoch,"
                "lease_owner,lease_expires_at,created_at,updated_at FROM intake_batches "
                "WHERE user_id=%s AND (status IN ('pending','slicing','solving','grading') OR updated_at>=%s) "
                "ORDER BY CASE WHEN status IN ('pending','slicing','solving','grading') THEN 0 ELSE 1 END,updated_at DESC,id DESC LIMIT 1",
                (user_id, threshold),
            )
            row = cursor.fetchone()
            return self._public(self._row(row)) if row else None
        finally:
            cursor.close()
            connection.close()

    def list_batches(self, *, user_id: str) -> list[IntakeBatch]:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT id,user_id,status,total_files,completed_files,total_items,completed_items,stage_completed_items,"
                "last_event_id,error_code,slicing_attempts,solving_attempts,grading_attempts,retry_at,slot_id,claim_epoch,"
                "lease_owner,lease_expires_at,created_at,updated_at FROM intake_batches "
                "WHERE user_id=%s ORDER BY created_at,id",
                (user_id,),
            )
            return [self._public(self._row(row)) for row in cursor.fetchall()]
        finally:
            cursor.close()
            connection.close()

    def fail_user_batches(self, *, user_id: str, error_code: str = "account_deleted") -> int:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) is None:
            raise ValueError("invalid intake batch error code")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute("SELECT slot_id FROM intake_worker_slots ORDER BY slot_id FOR UPDATE")
            if [int(row[0]) for row in cursor.fetchall()] != [1, 2]:
                raise RuntimeError("invalid_worker_slots")
            cursor.execute(
                "SELECT id,user_id,status,total_files,completed_files,total_items,completed_items,stage_completed_items,"
                "last_event_id,error_code,slicing_attempts,solving_attempts,grading_attempts,retry_at,slot_id,claim_epoch,"
                "lease_owner,lease_expires_at,created_at,updated_at FROM intake_batches "
                "WHERE user_id=%s AND status IN ('pending','slicing','solving','grading') ORDER BY id FOR UPDATE",
                (user_id,),
            )
            records = [self._row(row) for row in cursor.fetchall()]
            now = self._db_now_tx(cursor)
            for record in records:
                slot_id = record["slot_id"]
                lease_owner = record["lease_owner"]
                claim_epoch = record["claim_epoch"]
                if slot_id is not None:
                    cursor.execute(
                        "UPDATE intake_worker_slots SET batch_id=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=%s "
                        "WHERE slot_id=%s AND batch_id=%s AND claim_epoch=%s AND lease_owner=%s",
                        (now, slot_id, record["batch_id"], claim_epoch, lease_owner),
                    )
                    self._require_changed(cursor)
                record.update({
                    "status": "failed", "error_code": error_code, "slot_id": None,
                    "lease_owner": None, "lease_expires_at": None, "updated_at": now,
                })
                if slot_id is None:
                    cursor.execute(
                        "UPDATE intake_batches SET status='failed',error_code=%s,slot_id=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=%s "
                        "WHERE id=%s AND user_id=%s AND status IN ('pending','slicing','solving','grading') AND slot_id IS NULL",
                        (error_code, now, record["batch_id"], user_id),
                    )
                else:
                    cursor.execute(
                        "UPDATE intake_batches SET status='failed',error_code=%s,slot_id=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=%s "
                        "WHERE id=%s AND user_id=%s AND status IN ('pending','slicing','solving','grading') "
                        "AND slot_id=%s AND claim_epoch=%s AND lease_owner=%s",
                        (
                            error_code, now, record["batch_id"], user_id, slot_id,
                            claim_epoch, lease_owner,
                        ),
                    )
                self._require_changed(cursor)
                self._append_event_tx(cursor, record, "batch_failed", now)
            connection.commit()
            return len(records)
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def list_files(self, *, batch_id: str) -> list[IntakeBatchFile]:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT file_ordinal,file_id,object_key,content_sha256,media_type,byte_size "
                "FROM intake_batch_files WHERE batch_id=%s ORDER BY file_ordinal",
                (batch_id,),
            )
            return [
                IntakeBatchFile(batch_id, int(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5]))
                for row in cursor.fetchall()
            ]
        finally:
            cursor.close()
            connection.close()

    def list_events(self, *, user_id: str, batch_id: str, after_sequence: int) -> list[IntakeBatchEvent]:
        if not isinstance(after_sequence, int) or isinstance(after_sequence, bool) or after_sequence < 0:
            raise ValueError("invalid_event_cursor")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT last_event_id FROM intake_batches WHERE id=%s AND user_id=%s", (batch_id, user_id))
            high_water = cursor.fetchone()
            if not high_water:
                raise LookupError("batch not found")
            if after_sequence > int(high_water[0]):
                raise ValueError("invalid_event_cursor")
            cursor.execute(
                "SELECT event_sequence,event_type,data_json,created_at FROM intake_batch_events "
                "WHERE batch_id=%s AND event_sequence>%s ORDER BY event_sequence",
                (batch_id, after_sequence),
            )
            return [
                IntakeBatchEvent(batch_id, int(row[0]), str(row[1]), _json(row[2]), _aware(row[3]))
                for row in cursor.fetchall()
            ]
        finally:
            cursor.close()
            connection.close()

    def get_operation(self, claim: BatchClaim, operation_key: str) -> BatchOperation | None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            self._require_claim_tx(cursor, claim)
            cursor.execute(
                "SELECT stage,item_ordinal,result_json,created_at FROM intake_batch_operations "
                "WHERE batch_id=%s AND operation_key=%s AND status='completed'",
                (claim.batch_id, operation_key),
            )
            row = cursor.fetchone()
            connection.commit()
            return BatchOperation(claim.batch_id, operation_key, str(row[0]), int(row[1]), _json(row[2]), _aware(row[3])) if row else None
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def list_operations(self, *, user_id: str, batch_id: str, stage: str) -> list[BatchOperation]:
        if stage not in {"slicing", "solving", "grading"}:
            raise ValueError("invalid batch stage")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT o.operation_key,o.item_ordinal,o.result_json,o.created_at "
                "FROM intake_batch_operations o JOIN intake_batches b ON b.id=o.batch_id "
                "WHERE o.batch_id=%s AND b.user_id=%s AND o.stage=%s AND o.status='completed' "
                "ORDER BY o.item_ordinal,o.operation_key",
                (batch_id, user_id, stage),
            )
            return [
                BatchOperation(batch_id, str(row[0]), stage, int(row[1]), _json(row[2]), _aware(row[3]))
                for row in cursor.fetchall()
            ]
        finally:
            cursor.close()
            connection.close()

    def claim_next(self, *, worker_id: str, lease_seconds: int) -> BatchClaim | None:
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 64:
            raise ValueError("invalid worker id")
        if not isinstance(lease_seconds, int) or not 30 <= lease_seconds <= 3600:
            raise ValueError("invalid lease duration")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute(
                "SELECT b.id,b.user_id FROM intake_batches b "
                "WHERE b.status IN ('pending','slicing','solving','grading') "
                "AND b.retry_at<=UTC_TIMESTAMP(6) "
                "AND (b.lease_expires_at IS NULL OR b.lease_expires_at<=UTC_TIMESTAMP(6)) "
                "AND NOT EXISTS (SELECT 1 FROM account_deletions d WHERE d.user_id=b.user_id) "
                "ORDER BY b.retry_at,b.created_at,b.id LIMIT 1"
            )
            candidate = cursor.fetchone()
            if not candidate:
                connection.commit()
                return None
            candidate_batch_id, candidate_user_id = str(candidate[0]), str(candidate[1])
            cursor.execute("SELECT id FROM web_users WHERE id=%s FOR UPDATE", (candidate_user_id,))
            if not cursor.fetchone():
                connection.commit()
                return None
            cursor.execute("SELECT status FROM account_deletions WHERE user_id=%s FOR UPDATE", (candidate_user_id,))
            if cursor.fetchone():
                connection.commit()
                return None
            cursor.execute(
                "SELECT slot_id,batch_id,claim_epoch FROM intake_worker_slots "
                "WHERE batch_id IS NULL OR lease_expires_at<=UTC_TIMESTAMP(6) "
                "ORDER BY slot_id LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
            slot = cursor.fetchone()
            if not slot:
                connection.commit()
                return None
            slot_id, expired_batch_id = int(slot[0]), str(slot[1]) if slot[1] else None
            if expired_batch_id:
                cursor.execute(
                    "UPDATE intake_batches SET slot_id=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=UTC_TIMESTAMP(6) "
                    "WHERE id=%s AND slot_id=%s AND claim_epoch=%s AND lease_expires_at<=UTC_TIMESTAMP(6)",
                    (expired_batch_id, slot_id, int(slot[2])),
                )
                self._require_changed(cursor)
                cursor.execute(
                    "UPDATE intake_worker_slots SET batch_id=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=UTC_TIMESTAMP(6) "
                    "WHERE slot_id=%s AND batch_id=%s AND claim_epoch=%s AND lease_expires_at<=UTC_TIMESTAMP(6)",
                    (slot_id, expired_batch_id, int(slot[2])),
                )
                self._require_changed(cursor)
            cursor.execute(
                "SELECT id,user_id,status,claim_epoch,slicing_attempts,solving_attempts,grading_attempts "
                "FROM intake_batches WHERE id=%s AND user_id=%s "
                "AND status IN ('pending','slicing','solving','grading') "
                "AND retry_at<=UTC_TIMESTAMP(6) AND (lease_expires_at IS NULL OR lease_expires_at<=UTC_TIMESTAMP(6)) "
                "FOR UPDATE",
                (candidate_batch_id, candidate_user_id),
            )
            row = cursor.fetchone()
            if not row:
                connection.commit()
                return None
            batch_id, user_id, status, previous_epoch = str(row[0]), str(row[1]), str(row[2]), int(row[3])
            attempts = {"slicing": int(row[4]), "solving": int(row[5]), "grading": int(row[6])}
            attempt_stage = "slicing" if status == "pending" else status
            if attempts[attempt_stage] >= 3:
                record = self._load_batch_tx(cursor, batch_id=batch_id, user_id=user_id, lock=True)
                assert record is not None
                now = self._db_now_tx(cursor)
                record.update({"status": "failed", "error_code": "retry_exhausted", "updated_at": now})
                cursor.execute(
                    "UPDATE intake_batches SET status='failed',error_code='retry_exhausted',updated_at=%s "
                    "WHERE id=%s AND status=%s",
                    (now, batch_id, status),
                )
                self._require_changed(cursor)
                self._append_event_tx(cursor, record, "batch_failed", now)
                connection.commit()
                return None
            attempts[attempt_stage] += 1
            epoch = previous_epoch + 1
            cursor.execute(
                "UPDATE intake_batches SET slot_id=%s,claim_epoch=%s,lease_owner=%s,"
                "lease_expires_at=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND),"
                "slicing_attempts=%s,solving_attempts=%s,grading_attempts=%s,updated_at=UTC_TIMESTAMP(6) "
                "WHERE id=%s AND claim_epoch=%s AND status=%s",
                (slot_id, epoch, worker_id, lease_seconds, attempts["slicing"], attempts["solving"], attempts["grading"], batch_id, previous_epoch, status),
            )
            self._require_changed(cursor)
            cursor.execute(
                "UPDATE intake_worker_slots SET batch_id=%s,claim_epoch=%s,lease_owner=%s,"
                "lease_expires_at=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND),updated_at=UTC_TIMESTAMP(6) "
                "WHERE slot_id=%s AND batch_id IS NULL",
                (batch_id, epoch, worker_id, lease_seconds, slot_id),
            )
            self._require_changed(cursor)
            cursor.execute("SELECT lease_expires_at FROM intake_batches WHERE id=%s", (batch_id,))
            expires = cursor.fetchone()[0]
            connection.commit()
            return BatchClaim(batch_id, user_id, slot_id, epoch, worker_id, _aware(expires))
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def renew(self, claim: BatchClaim, *, lease_seconds: int) -> BatchClaim:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            self._require_claim_tx(cursor, claim)
            expires = self._renew_claim_tx(cursor, claim, lease_seconds=lease_seconds)
            connection.commit()
            return BatchClaim(claim.batch_id, claim.user_id, claim.slot_id, claim.claim_epoch, claim.lease_owner, _aware(expires))
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def transition(self, claim: BatchClaim, *, expected: str, target: str, total_items: int | None = None) -> IntakeBatch:
        if (expected, target) not in TRANSITIONS:
            raise RuntimeError("invalid_batch_transition")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            record = self._require_claim_tx(cursor, claim)
            if record["status"] != expected:
                raise RuntimeError("invalid_batch_transition")
            if expected == "slicing":
                if record["completed_files"] != record["total_files"] or not isinstance(total_items, int) or not 1 <= total_items <= 160:
                    raise RuntimeError("incomplete_batch_stage")
                record["total_items"] = total_items
            elif expected == "solving" and record["stage_completed_items"] != record["total_items"]:
                raise RuntimeError("incomplete_batch_stage")
            elif expected == "grading" and record["completed_items"] != record["total_items"]:
                raise RuntimeError("incomplete_batch_stage")
            attempts = {
                "slicing": record["slicing_attempts"], "solving": record["solving_attempts"], "grading": record["grading_attempts"],
            }
            if target in attempts:
                attempts[target] = max(1, attempts[target])
            record.update({
                "status": target, "stage_completed_items": 0,
                "slicing_attempts": attempts["slicing"], "solving_attempts": attempts["solving"], "grading_attempts": attempts["grading"],
            })
            cursor.execute(
                "UPDATE intake_batches SET status=%s,total_items=%s,stage_completed_items=0,"
                "slicing_attempts=%s,solving_attempts=%s,grading_attempts=%s,updated_at=UTC_TIMESTAMP(6) "
                "WHERE id=%s AND status=%s AND slot_id=%s AND claim_epoch=%s AND lease_owner=%s AND lease_expires_at>UTC_TIMESTAMP(6)",
                (target, record["total_items"], attempts["slicing"], attempts["solving"], attempts["grading"], claim.batch_id, expected, claim.slot_id, claim.claim_epoch, claim.lease_owner),
            )
            self._require_changed(cursor)
            cursor.execute("SELECT updated_at FROM intake_batches WHERE id=%s", (claim.batch_id,))
            record["updated_at"] = cursor.fetchone()[0]
            self._append_event_tx(cursor, record, TRANSITIONS[(expected, target)], record["updated_at"])
            if target in TERMINAL_STATES:
                self._release_tx(cursor, claim)
                record.update({"slot_id": None, "lease_owner": None, "lease_expires_at": None})
            connection.commit()
            return self._public(record)
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def record_operation(
        self,
        claim: BatchClaim,
        *,
        operation_key: str,
        stage: str,
        ordinal: int,
        result: dict[str, Any],
        completed_files_delta: int = 0,
        completed_items_delta: int = 0,
        event_data: dict[str, Any] | None = None,
    ) -> bool:
        _, created = self.run_fenced_operation(
            claim,
            operation_key=operation_key,
            stage=stage,
            ordinal=ordinal,
            action=lambda: (result, event_data),
            completed_files_delta=completed_files_delta,
            completed_items_delta=completed_items_delta,
        )
        return created

    def run_fenced_operation(
        self,
        claim: BatchClaim,
        *,
        operation_key: str,
        stage: str,
        ordinal: int,
        action: Callable[[], tuple[dict[str, Any], dict[str, Any] | None]],
        completed_files_delta: int = 0,
        completed_items_delta: int = 0,
    ) -> tuple[BatchOperation, bool]:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}:[1-9][0-9]{0,3}", operation_key):
            raise ValueError("invalid operation key")
        if stage not in {"slicing", "solving", "grading"} or not callable(action):
            raise ValueError("invalid batch operation")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            record = self._require_claim_tx(cursor, claim)
            if record["status"] != stage:
                raise RuntimeError("invalid_batch_transition")
            cursor.execute(
                "SELECT result_json,created_at FROM intake_batch_operations "
                "WHERE batch_id=%s AND operation_key=%s AND status='completed' FOR UPDATE",
                (claim.batch_id, operation_key),
            )
            existing = cursor.fetchone()
            if existing:
                connection.commit()
                return BatchOperation(
                    claim.batch_id, operation_key, stage, ordinal, _json(existing[0]), _aware(existing[1]),
                ), False
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

        result, event_data = action()
        if not isinstance(result, dict) or event_data is not None and not isinstance(event_data, dict):
            raise ValueError("invalid batch operation")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            record = self._require_claim_tx(cursor, claim)
            if record["status"] != stage:
                raise RuntimeError("invalid_batch_transition")
            cursor.execute(
                "SELECT result_json,created_at FROM intake_batch_operations "
                "WHERE batch_id=%s AND operation_key=%s AND status='completed' FOR UPDATE",
                (claim.batch_id, operation_key),
            )
            existing = cursor.fetchone()
            if existing:
                connection.commit()
                return BatchOperation(
                    claim.batch_id, operation_key, stage, ordinal, _json(existing[0]), _aware(existing[1]),
                ), False
            self._renew_claim_tx(cursor, claim, lease_seconds=300)
            next_files = record["completed_files"] + completed_files_delta
            next_items = record["completed_items"] + completed_items_delta
            if not 0 <= next_files <= record["total_files"]:
                raise RuntimeError("invalid_batch_progress")
            if record["total_items"] is not None and not 0 <= next_items <= record["total_items"]:
                raise RuntimeError("invalid_batch_progress")
            now = self._db_now_tx(cursor)
            cursor.execute(
                "INSERT INTO intake_batch_operations "
                "(batch_id,operation_key,stage,item_ordinal,status,result_json,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,'completed',%s,%s,%s)",
                (claim.batch_id, operation_key, stage, ordinal, json.dumps(result, ensure_ascii=False, separators=(",", ":")), now, now),
            )
            record.update({
                "completed_files": next_files,
                "completed_items": next_items,
                "stage_completed_items": record["stage_completed_items"] + 1,
                "updated_at": now,
            })
            cursor.execute(
                "UPDATE intake_batches SET completed_files=%s,completed_items=%s,stage_completed_items=%s,updated_at=%s "
                "WHERE id=%s AND status=%s AND slot_id=%s AND claim_epoch=%s AND lease_owner=%s AND lease_expires_at>UTC_TIMESTAMP(6)",
                (next_files, next_items, record["stage_completed_items"], now, claim.batch_id, stage, claim.slot_id, claim.claim_epoch, claim.lease_owner),
            )
            self._require_changed(cursor)
            self._append_event_tx(
                cursor,
                record,
                "item_completed" if stage == "grading" else "progress",
                now,
                event_data=event_data,
            )
            connection.commit()
            return BatchOperation(claim.batch_id, operation_key, stage, ordinal, result, _aware(now)), True
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def retry_or_fail(self, claim: BatchClaim, *, error_code: str, retryable: bool) -> IntakeBatch:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
            raise ValueError("invalid intake batch error code")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            record = self._require_claim_tx(cursor, claim)
            status = record["status"]
            attempts = record.get(f"{status}_attempts", 0)
            if retryable and status in {"slicing", "solving", "grading"} and attempts < 3:
                record["error_code"] = error_code
                delay = 5 if attempts <= 1 else 10
                cursor.execute(
                    "UPDATE intake_batches SET retry_at=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND),"
                    "error_code=%s,updated_at=UTC_TIMESTAMP(6) "
                    "WHERE id=%s AND status=%s AND slot_id=%s AND claim_epoch=%s AND lease_owner=%s AND lease_expires_at>UTC_TIMESTAMP(6)",
                    (delay, error_code, claim.batch_id, status, claim.slot_id, claim.claim_epoch, claim.lease_owner),
                )
                self._require_changed(cursor)
                cursor.execute("SELECT retry_at,updated_at FROM intake_batches WHERE id=%s", (claim.batch_id,))
                record["retry_at"], record["updated_at"] = cursor.fetchone()
                now = record["updated_at"]
                self._append_event_tx(cursor, record, "progress", now)
            else:
                cursor.execute(
                    "UPDATE intake_batches SET status='failed',error_code=%s,updated_at=UTC_TIMESTAMP(6) "
                    "WHERE id=%s AND status=%s AND slot_id=%s AND claim_epoch=%s AND lease_owner=%s AND lease_expires_at>UTC_TIMESTAMP(6)",
                    (error_code, claim.batch_id, status, claim.slot_id, claim.claim_epoch, claim.lease_owner),
                )
                self._require_changed(cursor)
                cursor.execute("SELECT updated_at FROM intake_batches WHERE id=%s", (claim.batch_id,))
                now = cursor.fetchone()[0]
                record.update({"status": "failed", "error_code": error_code, "updated_at": now})
                self._append_event_tx(cursor, record, "batch_failed", now)
            self._release_tx(cursor, claim)
            record.update({"slot_id": None, "lease_owner": None, "lease_expires_at": None})
            connection.commit()
            return self._public(record)
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    @staticmethod
    def _validate_create(file_ids: list[str], idempotency_key: str) -> None:
        if not isinstance(file_ids, list) or not 1 <= len(file_ids) <= 8:
            raise ValueError("one to eight files are required")
        if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None for value in file_ids):
            raise ValueError("invalid file id")
        if len(set(file_ids)) != len(file_ids):
            raise ValueError("duplicate file ids are not allowed")
        if not isinstance(idempotency_key, str) or IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise ValueError("invalid idempotency key")

    @staticmethod
    def _row(row: tuple[Any, ...]) -> dict[str, Any]:
        fields = (
            "batch_id", "user_id", "status", "total_files", "completed_files", "total_items",
            "completed_items", "stage_completed_items", "last_event_id", "error_code",
            "slicing_attempts", "solving_attempts", "grading_attempts", "retry_at", "slot_id",
            "claim_epoch", "lease_owner", "lease_expires_at", "created_at", "updated_at",
        )
        return dict(zip(fields, row, strict=True))

    def _load_batch_tx(self, cursor, *, batch_id: str, user_id: str, lock: bool) -> dict[str, Any] | None:
        cursor.execute(
            "SELECT id,user_id,status,total_files,completed_files,total_items,completed_items,stage_completed_items,"
            "last_event_id,error_code,slicing_attempts,solving_attempts,grading_attempts,retry_at,slot_id,claim_epoch,"
            "lease_owner,lease_expires_at,created_at,updated_at FROM intake_batches WHERE id=%s AND user_id=%s" + (" FOR UPDATE" if lock else ""),
            (batch_id, user_id),
        )
        row = cursor.fetchone()
        return self._row(row) if row else None

    def _require_claim_tx(self, cursor, claim: BatchClaim) -> dict[str, Any]:
        cursor.execute("SELECT id FROM web_users WHERE id=%s FOR UPDATE", (claim.user_id,))
        if not cursor.fetchone():
            raise RuntimeError("stale_claim")
        cursor.execute("SELECT status FROM account_deletions WHERE user_id=%s FOR UPDATE", (claim.user_id,))
        if cursor.fetchone():
            raise RuntimeError("stale_claim")
        cursor.execute(
            "SELECT slot_id FROM intake_worker_slots WHERE slot_id=%s AND batch_id=%s "
            "AND claim_epoch=%s AND lease_owner=%s AND lease_expires_at>UTC_TIMESTAMP(6) FOR UPDATE",
            (claim.slot_id, claim.batch_id, claim.claim_epoch, claim.lease_owner),
        )
        if not cursor.fetchone():
            raise RuntimeError("stale_claim")
        cursor.execute(
            "SELECT id,user_id,status,total_files,completed_files,total_items,completed_items,stage_completed_items,"
            "last_event_id,error_code,slicing_attempts,solving_attempts,grading_attempts,retry_at,slot_id,claim_epoch,"
            "lease_owner,lease_expires_at,created_at,updated_at FROM intake_batches "
            "WHERE id=%s AND user_id=%s AND slot_id=%s AND claim_epoch=%s AND lease_owner=%s "
            "AND status IN ('pending','slicing','solving','grading') AND lease_expires_at>UTC_TIMESTAMP(6) FOR UPDATE",
            (claim.batch_id, claim.user_id, claim.slot_id, claim.claim_epoch, claim.lease_owner),
        )
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("stale_claim")
        return self._row(row)

    def require_domain_claim_tx(self, cursor, claim: BatchClaim, *, stage: str) -> dict[str, Any]:
        if stage not in {"slicing", "solving", "grading"}:
            raise ValueError("invalid batch stage")
        record = self._require_claim_tx(cursor, claim)
        if record["status"] != stage:
            raise RuntimeError("invalid_batch_transition")
        self._renew_claim_tx(cursor, claim, lease_seconds=300)
        return record

    @staticmethod
    def _db_now_tx(cursor) -> datetime:
        cursor.execute("SELECT UTC_TIMESTAMP(6)")
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("database clock unavailable")
        return row[0]

    def _renew_claim_tx(self, cursor, claim: BatchClaim, *, lease_seconds: int) -> datetime:
        if not isinstance(lease_seconds, int) or not 30 <= lease_seconds <= 3600:
            raise ValueError("invalid lease duration")
        cursor.execute(
            "UPDATE intake_worker_slots SET lease_expires_at=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND),"
            "updated_at=UTC_TIMESTAMP(6) WHERE slot_id=%s AND batch_id=%s AND claim_epoch=%s "
            "AND lease_owner=%s AND lease_expires_at>UTC_TIMESTAMP(6)",
            (lease_seconds, claim.slot_id, claim.batch_id, claim.claim_epoch, claim.lease_owner),
        )
        self._require_changed(cursor)
        cursor.execute(
            "UPDATE intake_batches SET lease_expires_at=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND),"
            "updated_at=UTC_TIMESTAMP(6) WHERE id=%s AND user_id=%s AND slot_id=%s AND claim_epoch=%s "
            "AND lease_owner=%s AND lease_expires_at>UTC_TIMESTAMP(6)",
            (
                lease_seconds, claim.batch_id, claim.user_id, claim.slot_id,
                claim.claim_epoch, claim.lease_owner,
            ),
        )
        self._require_changed(cursor)
        cursor.execute("SELECT lease_expires_at FROM intake_batches WHERE id=%s", (claim.batch_id,))
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("stale_claim")
        return row[0]

    @staticmethod
    def _require_changed(cursor) -> None:
        if cursor.rowcount != 1:
            raise RuntimeError("stale_claim")

    def _append_event_tx(
        self,
        cursor,
        record: dict[str, Any],
        event_type: str,
        now: datetime,
        *,
        event_data: dict[str, Any] | None = None,
    ) -> None:
        sequence = int(record["last_event_id"]) + 1
        record["last_event_id"] = sequence
        data = self._snapshot(record)
        if event_data:
            data["item"] = json.loads(json.dumps(event_data, ensure_ascii=False))
        cursor.execute(
            "UPDATE intake_batches SET last_event_id=%s,updated_at=%s WHERE id=%s",
            (sequence, now, record["batch_id"]),
        )
        cursor.execute(
            "INSERT INTO intake_batch_events (batch_id,event_sequence,event_type,data_json,created_at) VALUES (%s,%s,%s,%s,%s)",
            (record["batch_id"], sequence, event_type, json.dumps(data, ensure_ascii=False, separators=(",", ":")), now),
        )

    @staticmethod
    def _release_tx(cursor, claim: BatchClaim) -> None:
        cursor.execute(
            "UPDATE intake_worker_slots SET batch_id=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=UTC_TIMESTAMP(6) "
            "WHERE slot_id=%s AND batch_id=%s AND claim_epoch=%s AND lease_owner=%s",
            (claim.slot_id, claim.batch_id, claim.claim_epoch, claim.lease_owner),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("stale_claim")
        cursor.execute(
            "UPDATE intake_batches SET slot_id=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=UTC_TIMESTAMP(6) "
            "WHERE id=%s AND user_id=%s AND slot_id=%s AND claim_epoch=%s AND lease_owner=%s",
            (claim.batch_id, claim.user_id, claim.slot_id, claim.claim_epoch, claim.lease_owner),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("stale_claim")

    @staticmethod
    def _snapshot(record: dict[str, Any]) -> dict[str, Any]:
        status = str(record["status"])
        total = int(record["total_items"]) if record["total_items"] is not None else int(record["total_files"])
        current = int(record["completed_items"]) if record["total_items"] is not None else int(record["completed_files"])
        return {
            "schema": "intake-batch-event/v1", "batch_id": str(record["batch_id"]), "status": status,
            "current_stage": None if status in TERMINAL_STATES else status,
            "total_files": int(record["total_files"]), "completed_files": int(record["completed_files"]),
            "total_items": int(record["total_items"]) if record["total_items"] is not None else None,
            "completed_items": int(record["completed_items"]), "stage_completed_items": int(record["stage_completed_items"]),
            "terminal": status in TERMINAL_STATES, "last_event_id": int(record["last_event_id"]),
            "error_code": str(record["error_code"]) if record["error_code"] else None,
            "current": current, "total": total, "stage": None if status in TERMINAL_STATES else status,
        }

    @staticmethod
    def _public(record: dict[str, Any]) -> IntakeBatch:
        status = str(record["status"])
        return IntakeBatch(
            str(record["batch_id"]), str(record["user_id"]), status, int(record["total_files"]),
            int(record["completed_files"]), int(record["total_items"]) if record["total_items"] is not None else None,
            int(record["completed_items"]), int(record["stage_completed_items"]), status in TERMINAL_STATES,
            int(record["last_event_id"]), str(record["error_code"]) if record["error_code"] else None,
            _aware(record["created_at"]), _aware(record["updated_at"]),
        )
