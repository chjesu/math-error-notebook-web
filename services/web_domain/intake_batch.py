"""Durable, fenced batch intake state and the bounded worker engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from threading import Event, RLock, Thread
from time import monotonic
from typing import Any, Callable, Protocol
import uuid


NONTERMINAL_STATES = ("pending", "slicing", "solving", "grading")
TERMINAL_STATES = ("completed", "failed")
TRANSITIONS = {
    ("pending", "slicing"): "progress",
    ("slicing", "solving"): "progress",
    ("solving", "grading"): "progress",
    ("grading", "completed"): "batch_completed",
}
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class IntakeBatch:
    batch_id: str
    user_id: str
    status: str
    total_files: int
    completed_files: int
    total_items: int | None
    completed_items: int
    stage_completed_items: int
    terminal: bool
    last_event_id: int
    error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class IntakeBatchFile:
    batch_id: str
    ordinal: int
    file_id: str
    object_key: str
    content_sha256: str
    media_type: str
    byte_size: int


@dataclass(frozen=True)
class IntakeBatchEvent:
    batch_id: str
    sequence: int
    event_type: str
    data: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class BatchClaim:
    batch_id: str
    user_id: str
    slot_id: int
    claim_epoch: int
    lease_owner: str
    lease_expires_at: datetime


@dataclass(frozen=True)
class BatchOperation:
    batch_id: str
    operation_key: str
    stage: str
    ordinal: int
    result: dict[str, Any]
    created_at: datetime


class IntakeBatchFailure(Exception):
    def __init__(self, code: str, *, retryable: bool) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("invalid intake batch error code")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class IntakeStageProcessor(Protocol):
    def process_stage(
        self,
        stage: str,
        claim: BatchClaim,
        repository: "InMemoryIntakeBatchRepository",
    ) -> int | None: ...


class InMemoryIntakeBatchRepository:
    """Thread-safe reference adapter; MySQL implements the same public contract."""

    def __init__(
        self,
        file_lookup: Callable[..., Any],
        *,
        clock: Callable[[], datetime] = utcnow,
        max_files: int = 8,
        admission_check: Callable[[str], bool] | None = None,
    ) -> None:
        self._file_lookup = file_lookup
        self._clock = clock
        self._max_files = max_files
        self._admission_check = admission_check or (lambda _user_id: True)
        self._lock = RLock()
        self._batches: dict[str, dict[str, Any]] = {}
        self._files: dict[str, list[IntakeBatchFile]] = {}
        self._events: dict[str, list[IntakeBatchEvent]] = {}
        self._operations: dict[tuple[str, str], BatchOperation] = {}
        self._idempotency: dict[tuple[str, str, str], tuple[str, str]] = {}
        self._slots: dict[int, dict[str, Any] | None] = {1: None, 2: None}
        self._deleted_users: set[str] = set()

    def create_batch(
        self,
        *,
        user_id: str,
        file_ids: list[str],
        idempotency_key: str,
    ) -> tuple[IntakeBatch, bool]:
        if not isinstance(file_ids, list) or not 1 <= len(file_ids) <= self._max_files:
            raise ValueError("one to eight files are required")
        if any(not isinstance(file_id, str) or not re.fullmatch(r"[0-9a-f]{32}", file_id) for file_id in file_ids):
            raise ValueError("invalid file id")
        if len(set(file_ids)) != len(file_ids):
            raise ValueError("duplicate file ids are not allowed")
        if not isinstance(idempotency_key, str) or IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise ValueError("invalid idempotency key")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"schema": "intake-batch-create/v1", "file_ids": file_ids},
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        scoped_key = (user_id, "intake-batch-create/v1", idempotency_key)
        with self._lock:
            previous = self._idempotency.get(scoped_key)
            if previous:
                if previous[1] != fingerprint:
                    raise RuntimeError("idempotency_conflict")
                return self._public(self._batches[previous[0]]), False
            if user_id in self._deleted_users or not self._admission_check(user_id):
                raise RuntimeError("account_deleted")
            active_count = sum(
                record["user_id"] == user_id and record["status"] in NONTERMINAL_STATES
                for record in self._batches.values()
            )
            if active_count >= 3:
                raise RuntimeError("batch_limit_reached")
            recent_count = sum(
                record["user_id"] == user_id and record["created_at"] >= self._clock() - timedelta(hours=24)
                for record in self._batches.values()
            )
            if recent_count >= 60:
                raise RuntimeError("batch_rate_limited")
            snapshots: list[IntakeBatchFile] = []
            batch_id = uuid.uuid4().hex
            for ordinal, file_id in enumerate(file_ids, 1):
                record = self._file_lookup(user_id=user_id, file_id=file_id)
                if (
                    record is None
                    or record.status != "ready"
                    or record.purpose != "question_image"
                    or record.media_type not in {"image/png", "image/jpeg"}
                ):
                    raise LookupError("batch input file not found")
                snapshots.append(IntakeBatchFile(
                    batch_id, ordinal, record.file_id, record.object_key,
                    record.content_sha256, record.media_type, record.byte_size,
                ))
            now = self._clock()
            record = {
                "batch_id": batch_id,
                "user_id": user_id,
                "request_sha256": fingerprint,
                "status": "pending",
                "total_files": len(snapshots),
                "completed_files": 0,
                "total_items": None,
                "completed_items": 0,
                "stage_completed_items": 0,
                "last_event_id": 0,
                "error_code": None,
                "retry_at": now,
                "attempts": {"slicing": 0, "solving": 0, "grading": 0},
                "slot_id": None,
                "claim_epoch": 0,
                "lease_owner": None,
                "lease_expires_at": None,
                "created_at": now,
                "updated_at": now,
            }
            self._batches[batch_id] = record
            self._files[batch_id] = snapshots
            self._events[batch_id] = []
            self._idempotency[scoped_key] = (batch_id, fingerprint)
            self._append_event(record, "progress")
            return self._public(record), True

    def get_batch(self, *, user_id: str, batch_id: str) -> IntakeBatch | None:
        with self._lock:
            record = self._batches.get(batch_id)
            return self._public(record) if record and record["user_id"] == user_id else None

    def find_active_batch(self, *, user_id: str) -> IntakeBatch | None:
        with self._lock:
            records = [
                record for record in self._batches.values()
                if record["user_id"] == user_id and record["status"] in NONTERMINAL_STATES
            ]
            record = max(records, key=lambda item: (item["updated_at"], item["batch_id"])) if records else None
            return self._public(record) if record else None

    def recovery_cursor(self) -> datetime:
        return self._clock()

    def find_recoverable_batch(self, *, user_id: str, updated_after: datetime) -> IntakeBatch | None:
        if not isinstance(updated_after, datetime) or updated_after.tzinfo is None:
            raise ValueError("invalid recovery cursor")
        with self._lock:
            active = [
                record for record in self._batches.values()
                if record["user_id"] == user_id and record["status"] in NONTERMINAL_STATES
            ]
            records = active or [
                record for record in self._batches.values()
                if record["user_id"] == user_id and record["updated_at"] >= updated_after
            ]
            record = max(records, key=lambda item: (item["updated_at"], item["batch_id"])) if records else None
            return self._public(record) if record else None

    def list_batches(self, *, user_id: str) -> list[IntakeBatch]:
        with self._lock:
            return [
                self._public(record)
                for record in sorted(
                    (item for item in self._batches.values() if item["user_id"] == user_id),
                    key=lambda item: (item["created_at"], item["batch_id"]),
                )
            ]

    def fail_user_batches(self, *, user_id: str, error_code: str = "account_deleted") -> int:
        with self._lock:
            self._deleted_users.add(user_id)
            return self._fail_user_batches_locked(user_id=user_id, error_code=error_code)

    def begin_user_deletion(self, *, user_id: str, marker_action: Callable[[], Any]) -> Any:
        if not callable(marker_action):
            raise ValueError("deletion marker action is required")
        with self._lock:
            self._deleted_users.add(user_id)
            marker = marker_action()
            self._fail_user_batches_locked(user_id=user_id, error_code="account_deleted")
            return marker

    def _fail_user_batches_locked(self, *, user_id: str, error_code: str) -> int:
        changed = 0
        for record in self._batches.values():
            if record["user_id"] != user_id or record["status"] not in NONTERMINAL_STATES:
                continue
            assignment = self._slots.get(record["slot_id"]) if record["slot_id"] else None
            if assignment and assignment["batch_id"] == record["batch_id"]:
                self._slots[record["slot_id"]] = None
            record.update({
                "status": "failed", "error_code": error_code, "slot_id": None,
                "lease_owner": None, "lease_expires_at": None, "updated_at": self._clock(),
            })
            self._append_event(record, "batch_failed")
            changed += 1
        return changed

    def list_files(self, *, batch_id: str) -> list[IntakeBatchFile]:
        with self._lock:
            return list(self._files.get(batch_id, ()))

    def list_events(
        self,
        *,
        user_id: str,
        batch_id: str,
        after_sequence: int,
    ) -> list[IntakeBatchEvent]:
        if not isinstance(after_sequence, int) or isinstance(after_sequence, bool) or after_sequence < 0:
            raise ValueError("invalid_event_cursor")
        with self._lock:
            record = self._batches.get(batch_id)
            if not record or record["user_id"] != user_id:
                raise LookupError("batch not found")
            if after_sequence > record["last_event_id"]:
                raise ValueError("invalid_event_cursor")
            return [event for event in self._events[batch_id] if event.sequence > after_sequence]

    def get_operation(self, claim: BatchClaim, operation_key: str) -> BatchOperation | None:
        with self._lock:
            self._require_claim(claim)
            return self._operations.get((claim.batch_id, operation_key))

    def list_operations(self, *, user_id: str, batch_id: str, stage: str) -> list[BatchOperation]:
        with self._lock:
            record = self._batches.get(batch_id)
            if not record or record["user_id"] != user_id:
                raise LookupError("batch not found")
            return sorted(
                (
                    operation for (owner_batch, _), operation in self._operations.items()
                    if owner_batch == batch_id and operation.stage == stage
                ),
                key=lambda operation: (operation.ordinal, operation.operation_key),
            )

    def claim_next(self, *, worker_id: str, lease_seconds: int) -> BatchClaim | None:
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 64:
            raise ValueError("invalid worker id")
        if not isinstance(lease_seconds, int) or not 30 <= lease_seconds <= 3600:
            raise ValueError("invalid lease duration")
        with self._lock:
            now = self._clock()
            for slot_id, assignment in self._slots.items():
                if assignment and assignment["lease_expires_at"] <= now:
                    self._slots[slot_id] = None
            slot_id = next((value for value, assignment in self._slots.items() if assignment is None), None)
            if slot_id is None:
                return None
            eligible = sorted(
                (
                    record for record in self._batches.values()
                    if record["status"] in NONTERMINAL_STATES
                    and record["user_id"] not in self._deleted_users
                    and self._admission_check(record["user_id"])
                    and record["retry_at"] <= now
                    and (record["lease_expires_at"] is None or record["lease_expires_at"] <= now)
                ),
                key=lambda record: (record["retry_at"], record["created_at"], record["batch_id"]),
            )
            if not eligible:
                return None
            record = eligible[0]
            attempt_stage = "slicing" if record["status"] == "pending" else record["status"]
            if record["attempts"][attempt_stage] >= 3:
                record.update({"status": "failed", "error_code": "retry_exhausted", "updated_at": now})
                self._append_event(record, "batch_failed")
                return None
            record["attempts"][attempt_stage] += 1
            record["claim_epoch"] += 1
            expires = now + timedelta(seconds=lease_seconds)
            record.update({
                "slot_id": slot_id,
                "lease_owner": worker_id,
                "lease_expires_at": expires,
                "updated_at": now,
            })
            assignment = {
                "batch_id": record["batch_id"],
                "claim_epoch": record["claim_epoch"],
                "lease_owner": worker_id,
                "lease_expires_at": expires,
            }
            self._slots[slot_id] = assignment
            return BatchClaim(
                record["batch_id"], record["user_id"], slot_id,
                record["claim_epoch"], worker_id, expires,
            )

    def renew(self, claim: BatchClaim, *, lease_seconds: int) -> BatchClaim:
        with self._lock:
            record = self._require_claim(claim)
            expires = self._clock() + timedelta(seconds=lease_seconds)
            record["lease_expires_at"] = expires
            record["updated_at"] = self._clock()
            assert record["slot_id"] is not None
            self._slots[record["slot_id"]]["lease_expires_at"] = expires
            return BatchClaim(
                claim.batch_id, claim.user_id, claim.slot_id,
                claim.claim_epoch, claim.lease_owner, expires,
            )

    def transition(
        self,
        claim: BatchClaim,
        *,
        expected: str,
        target: str,
        total_items: int | None = None,
    ) -> IntakeBatch:
        if (expected, target) not in TRANSITIONS:
            raise RuntimeError("invalid_batch_transition")
        with self._lock:
            record = self._require_claim(claim)
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
            record["status"] = target
            record["stage_completed_items"] = 0
            record["updated_at"] = self._clock()
            if target in {"slicing", "solving", "grading"}:
                record["attempts"][target] = max(1, record["attempts"][target])
            self._append_event(record, TRANSITIONS[(expected, target)])
            if target in TERMINAL_STATES:
                self._release(record, claim)
            return self._public(record)

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

    def run_fenced_action(self, claim: BatchClaim, *, stage: str, action: Callable[[], Any]) -> Any:
        if stage not in {"slicing", "solving", "grading"}:
            raise ValueError("invalid batch stage")
        with self._lock:
            record = self._require_claim(claim)
            if record["status"] != stage:
                raise RuntimeError("invalid_batch_transition")
            return action()

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
        with self._lock:
            record = self._require_claim(claim)
            if record["status"] != stage:
                raise RuntimeError("invalid_batch_transition")
            key = (claim.batch_id, operation_key)
            if key in self._operations:
                return self._operations[key], False
            result, event_data = action()
            if not isinstance(result, dict) or event_data is not None and not isinstance(event_data, dict):
                raise ValueError("invalid batch operation")
            next_files = record["completed_files"] + completed_files_delta
            next_items = record["completed_items"] + completed_items_delta
            if not 0 <= next_files <= record["total_files"]:
                raise RuntimeError("invalid_batch_progress")
            if record["total_items"] is not None and not 0 <= next_items <= record["total_items"]:
                raise RuntimeError("invalid_batch_progress")
            now = self._clock()
            self._operations[key] = BatchOperation(
                claim.batch_id, operation_key, stage, ordinal,
                json.loads(json.dumps(result, ensure_ascii=False)), now,
            )
            record["completed_files"] = next_files
            record["completed_items"] = next_items
            record["stage_completed_items"] += 1
            record["updated_at"] = now
            self._append_event(
                record,
                "item_completed" if stage == "grading" else "progress",
                event_data=event_data,
            )
            return self._operations[key], True

    def retry_or_fail(self, claim: BatchClaim, *, error_code: str, retryable: bool) -> IntakeBatch:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
            raise ValueError("invalid intake batch error code")
        with self._lock:
            record = self._require_claim(claim)
            status = record["status"]
            attempts = record["attempts"].get(status, 0)
            if retryable and status in {"slicing", "solving", "grading"} and attempts < 3:
                delay = 5 if attempts <= 1 else 10
                record["retry_at"] = self._clock() + timedelta(seconds=delay)
                record["error_code"] = error_code
                record["updated_at"] = self._clock()
                self._append_event(record, "progress")
                self._release(record, claim)
                return self._public(record)
            record["status"] = "failed"
            record["error_code"] = error_code
            record["updated_at"] = self._clock()
            self._append_event(record, "batch_failed")
            self._release(record, claim)
            return self._public(record)

    def _require_claim(self, claim: BatchClaim) -> dict[str, Any]:
        record = self._batches.get(claim.batch_id)
        now = self._clock()
        assignment = self._slots.get(claim.slot_id)
        if (
            not record
            or record["user_id"] != claim.user_id
            or record["user_id"] in self._deleted_users
            or not self._admission_check(record["user_id"])
            or record["status"] in TERMINAL_STATES
            or record["slot_id"] != claim.slot_id
            or record["claim_epoch"] != claim.claim_epoch
            or record["lease_owner"] != claim.lease_owner
            or record["lease_expires_at"] is None
            or record["lease_expires_at"] <= now
            or not assignment
            or assignment["batch_id"] != claim.batch_id
            or assignment["claim_epoch"] != claim.claim_epoch
        ):
            raise RuntimeError("stale_claim")
        return record

    def _release(self, record: dict[str, Any], claim: BatchClaim) -> None:
        assignment = self._slots.get(claim.slot_id)
        if assignment and assignment["batch_id"] == claim.batch_id and assignment["claim_epoch"] == claim.claim_epoch:
            self._slots[claim.slot_id] = None
        record["slot_id"] = None
        record["lease_owner"] = None
        record["lease_expires_at"] = None

    def _append_event(
        self,
        record: dict[str, Any],
        event_type: str,
        *,
        event_data: dict[str, Any] | None = None,
    ) -> None:
        record["last_event_id"] += 1
        data = self._snapshot(record)
        if event_data:
            data["item"] = json.loads(json.dumps(event_data, ensure_ascii=False))
        event = IntakeBatchEvent(
            record["batch_id"],
            record["last_event_id"],
            event_type,
            data,
            self._clock(),
        )
        self._events[record["batch_id"]].append(event)

    @staticmethod
    def _snapshot(record: dict[str, Any]) -> dict[str, Any]:
        total = record["total_items"] if record["total_items"] is not None else record["total_files"]
        current = record["completed_items"] if record["total_items"] is not None else record["completed_files"]
        return {
            "schema": "intake-batch-event/v1",
            "batch_id": record["batch_id"],
            "status": record["status"],
            "current_stage": None if record["status"] in TERMINAL_STATES else record["status"],
            "total_files": record["total_files"],
            "completed_files": record["completed_files"],
            "total_items": record["total_items"],
            "completed_items": record["completed_items"],
            "stage_completed_items": record["stage_completed_items"],
            "terminal": record["status"] in TERMINAL_STATES,
            "last_event_id": record["last_event_id"],
            "error_code": record["error_code"],
            "current": current,
            "total": total,
            "stage": None if record["status"] in TERMINAL_STATES else record["status"],
        }

    @staticmethod
    def _public(record: dict[str, Any]) -> IntakeBatch:
        return IntakeBatch(
            record["batch_id"], record["user_id"], record["status"],
            record["total_files"], record["completed_files"], record["total_items"],
            record["completed_items"], record["stage_completed_items"],
            record["status"] in TERMINAL_STATES, record["last_event_id"],
            record["error_code"], record["created_at"], record["updated_at"],
        )


class IntakeBatchEngine:
    """Two-worker dispatcher; repository slots keep the limit deployment-wide."""

    def __init__(
        self,
        repository: InMemoryIntakeBatchRepository,
        processor: IntakeStageProcessor,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 300,
        scan_interval: float = 0.5,
    ) -> None:
        self.repository = repository
        self.processor = processor
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:16]}"
        self.lease_seconds = lease_seconds
        self.scan_interval = scan_interval
        self._stop = Event()
        self._threads: list[Thread] = []
        self._lifecycle_lock = RLock()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._threads:
                return
            self._stop.clear()
            self._threads = [
                Thread(target=self._worker_loop, args=(f"{self.worker_id}-{index}",), daemon=True, name=f"intake-batch-{index}")
                for index in (1, 2)
            ]
            for thread in self._threads:
                thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        with self._lifecycle_lock:
            threads = list(self._threads)
            self._stop.set()
        deadline = monotonic() + timeout
        for thread in threads:
            thread.join(max(0.0, deadline - monotonic()))
        with self._lifecycle_lock:
            self._threads = []

    def run_once(self) -> bool:
        return self._run_once(self.worker_id)

    def _worker_loop(self, worker_id: str) -> None:
        while not self._stop.is_set():
            worked = self._run_once(worker_id)
            if not worked:
                self._stop.wait(self.scan_interval)

    def _run_once(self, worker_id: str) -> bool:
        claim = self.repository.claim_next(worker_id=worker_id, lease_seconds=self.lease_seconds)
        if claim is None:
            return False
        heartbeat_stop = Event()
        heartbeat_failed = Event()
        heartbeat = Thread(
            target=self._heartbeat,
            args=(claim, heartbeat_stop, heartbeat_failed),
            daemon=True,
            name="intake-batch-heartbeat",
        )
        heartbeat.start()
        try:
            current = self.repository.get_batch(user_id=claim.user_id, batch_id=claim.batch_id)
            if current is None:
                raise IntakeBatchFailure("batch_missing", retryable=False)
            if current.status == "pending":
                current = self.repository.transition(claim, expected="pending", target="slicing")
            while current.status in {"slicing", "solving", "grading"}:
                if heartbeat_failed.is_set():
                    raise RuntimeError("stale_claim")
                stage = current.status
                total_items = self.processor.process_stage(stage, claim, self.repository)
                if heartbeat_failed.is_set():
                    raise RuntimeError("stale_claim")
                if stage == "slicing":
                    current = self.repository.transition(
                        claim, expected="slicing", target="solving", total_items=total_items,
                    )
                elif stage == "solving":
                    current = self.repository.transition(claim, expected="solving", target="grading")
                else:
                    current = self.repository.transition(claim, expected="grading", target="completed")
            return True
        except IntakeBatchFailure as exc:
            self._record_failure(claim, error_code=exc.code, retryable=exc.retryable)
            return True
        except RuntimeError as exc:
            if str(exc) == "stale_claim":
                return True
            self._record_failure(claim, error_code="pipeline_failed", retryable=True)
            return True
        except Exception:
            self._record_failure(claim, error_code="pipeline_failed", retryable=True)
            return True
        finally:
            heartbeat_stop.set()
            heartbeat.join(0.2)

    def _heartbeat(self, claim: BatchClaim, stop: Event, failed: Event) -> None:
        interval = min(60.0, max(10.0, self.lease_seconds / 3))
        current = claim
        while not stop.wait(interval):
            try:
                current = self.repository.renew(current, lease_seconds=self.lease_seconds)
            except Exception:
                failed.set()
                return

    def _record_failure(self, claim: BatchClaim, *, error_code: str, retryable: bool) -> None:
        try:
            self.repository.retry_or_fail(claim, error_code=error_code, retryable=retryable)
        except RuntimeError as exc:
            if str(exc) != "stale_claim":
                raise
