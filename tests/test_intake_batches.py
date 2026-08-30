from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from PIL import Image

from services.web_domain import InMemoryNotebookStore, NotebookService, Question, cross_validate_reference
from services.web_domain.intake_batch import (
    BatchClaim,
    InMemoryIntakeBatchRepository,
    IntakeBatchEngine,
    IntakeBatchFailure,
)
from services.web_app.intake_pipeline import NotebookIntakeBatchProcessor
from services.web_app.codex_model import ModelUnavailableError
from tests.image_fixtures import png_bytes


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 30, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


class IntakeBatchRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.store = InMemoryNotebookStore()
        self.service = NotebookService(self.store, Path(self.temp.name))
        self.clock = MutableClock()
        self.repo = InMemoryIntakeBatchRepository(self.store.get_file, clock=self.clock)
        self.file_ids = [
            self.service.upload(
                user_id="a" * 32,
                purpose="question_image",
                original_name=f"question-{index}.png",
                content=png_bytes(color=(index, 0, 0)),
                idempotency_key=f"upload-{index}",
            ).file_id
            for index in (1, 2, 3)
        ]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_creation_snapshots_files_and_replays_one_idempotent_response(self) -> None:
        first, created = self.repo.create_batch(
            user_id="a" * 32,
            file_ids=self.file_ids[:2],
            idempotency_key="batch-one",
        )
        replay, replay_created = self.repo.create_batch(
            user_id="a" * 32,
            file_ids=self.file_ids[:2],
            idempotency_key="batch-one",
        )

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first, replay)
        self.assertEqual(first.status, "pending")
        self.assertEqual(first.total_files, 2)
        self.assertEqual(first.last_event_id, 1)
        snapshots = self.repo.list_files(batch_id=first.batch_id)
        self.assertEqual([item.ordinal for item in snapshots], [1, 2])
        self.assertTrue(all(len(item.content_sha256) == 64 for item in snapshots))
        with self.assertRaisesRegex(RuntimeError, "idempotency_conflict"):
            self.repo.create_batch(
                user_id="a" * 32,
                file_ids=list(reversed(self.file_ids[:2])),
                idempotency_key="batch-one",
            )
        self.assertIsNone(self.repo.get_batch(user_id="b" * 32, batch_id=first.batch_id))

    def test_creation_caps_active_and_daily_work_but_keeps_exact_replay(self) -> None:
        user_id = "a" * 32
        active = [
            self.repo.create_batch(
                user_id=user_id,
                file_ids=[self.file_ids[0]],
                idempotency_key=f"active-{index}",
            )[0]
            for index in range(3)
        ]

        replay, created = self.repo.create_batch(
            user_id=user_id,
            file_ids=[self.file_ids[0]],
            idempotency_key="active-0",
        )
        self.assertFalse(created)
        self.assertEqual(replay.batch_id, active[0].batch_id)
        with self.assertRaisesRegex(RuntimeError, "batch_limit_reached"):
            self.repo.create_batch(
                user_id=user_id,
                file_ids=[self.file_ids[0]],
                idempotency_key="active-overflow",
            )

        for index in range(3):
            claim = self.repo.claim_next(worker_id=f"finish-active-{index}", lease_seconds=300)
            assert claim is not None
            self.repo.retry_or_fail(claim, error_code="test_finished", retryable=False)
        for index in range(3, 60):
            batch = self.repo.create_batch(
                user_id=user_id,
                file_ids=[self.file_ids[0]],
                idempotency_key=f"daily-{index}",
            )[0]
            claim = self.repo.claim_next(worker_id=f"finish-daily-{index}", lease_seconds=300)
            assert claim is not None
            self.assertEqual(claim.batch_id, batch.batch_id)
            self.repo.retry_or_fail(claim, error_code="test_finished", retryable=False)

        replay, created = self.repo.create_batch(
            user_id=user_id,
            file_ids=[self.file_ids[0]],
            idempotency_key="daily-59",
        )
        self.assertFalse(created)
        self.assertEqual(replay.status, "failed")
        with self.assertRaisesRegex(RuntimeError, "batch_rate_limited"):
            self.repo.create_batch(
                user_id=user_id,
                file_ids=[self.file_ids[0]],
                idempotency_key="daily-overflow",
            )

        self.clock.advance(hours=24, seconds=1)
        accepted, created = self.repo.create_batch(
            user_id=user_id,
            file_ids=[self.file_ids[0]],
            idempotency_key="daily-next-window",
        )
        self.assertTrue(created)
        self.assertEqual(accepted.status, "pending")

    def test_deletion_marker_closes_batch_admission_before_cancellation_scan(self) -> None:
        user_id = "a" * 32
        self.store.begin_user_deletion(user_id=user_id)

        with self.assertRaisesRegex(RuntimeError, "account_deleted"):
            self.store.batch_repository.create_batch(
                user_id=user_id,
                file_ids=[self.file_ids[0]],
                idempotency_key="after-deletion-marker",
            )

    def test_expired_claim_cannot_mutate_the_domain_store(self) -> None:
        user_id = "a" * 32
        self.store.batch_repository = InMemoryIntakeBatchRepository(
            self.store.get_file,
            clock=self.clock,
            admission_check=lambda owner: owner not in self.store.account_deletions,
        )
        batch = self.store.batch_repository.create_batch(
            user_id=user_id,
            file_ids=[self.file_ids[0]],
            idempotency_key="domain-fence",
        )[0]
        claim = self.store.batch_repository.claim_next(worker_id="domain-worker", lease_seconds=300)
        assert claim is not None
        self.store.batch_repository.transition(claim, expected="pending", target="slicing")
        self.clock.advance(seconds=301)

        with self.assertRaisesRegex(RuntimeError, "stale_claim"):
            self.store.create_intake(
                user_id=user_id,
                file_id=self.file_ids[0],
                idempotency_key=f"batch-slice-{batch.batch_id}-1",
                batch_claim=claim,
                batch_stage="slicing",
            )
        self.assertEqual(self.store.get_file_intakes(user_id=user_id, file_id=self.file_ids[0]), [])

    def test_two_slots_fence_stale_writes_and_third_claim(self) -> None:
        batches = [
            self.repo.create_batch(user_id="a" * 32, file_ids=[file_id], idempotency_key=f"batch-{index}")[0]
            for index, file_id in enumerate(self.file_ids, 1)
        ]
        first = self.repo.claim_next(worker_id="worker-a", lease_seconds=300)
        second = self.repo.claim_next(worker_id="worker-b", lease_seconds=300)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(self.repo.claim_next(worker_id="worker-c", lease_seconds=300))

        assert first is not None
        started = self.repo.transition(first, expected="pending", target="slicing")
        self.assertEqual(started.status, "slicing")
        self.clock.advance(seconds=301)
        replacement = self.repo.claim_next(worker_id="worker-c", lease_seconds=300)
        self.assertIsNotNone(replacement)
        assert replacement is not None
        self.assertEqual(replacement.batch_id, first.batch_id)
        self.assertGreater(replacement.claim_epoch, first.claim_epoch)
        with self.assertRaisesRegex(RuntimeError, "stale_claim"):
            self.repo.record_operation(
                first,
                operation_key="slice:1",
                stage="slicing",
                ordinal=1,
                result={"intake_ids": ["x"]},
                completed_files_delta=1,
            )
        self.assertEqual(self.repo.get_batch(user_id="a" * 32, batch_id=batches[0].batch_id).completed_files, 0)

        callback_ran = False

        def stale_callback() -> None:
            nonlocal callback_ran
            callback_ran = True

        with self.assertRaisesRegex(RuntimeError, "stale_claim"):
            self.repo.run_fenced_action(first, stage="slicing", action=stale_callback)
        self.assertFalse(callback_ran)

    def test_operation_commit_and_event_are_idempotent(self) -> None:
        batch = self.repo.create_batch(user_id="a" * 32, file_ids=[self.file_ids[0]], idempotency_key="ops")[0]
        claim = self.repo.claim_next(worker_id="worker", lease_seconds=300)
        assert claim is not None
        self.repo.transition(claim, expected="pending", target="slicing")
        created = self.repo.record_operation(
            claim,
            operation_key="slice:1",
            stage="slicing",
            ordinal=1,
            result={"intake_ids": ["i1", "i2"]},
            completed_files_delta=1,
        )
        duplicate = self.repo.record_operation(
            claim,
            operation_key="slice:1",
            stage="slicing",
            ordinal=1,
            result={"intake_ids": ["ignored"]},
            completed_files_delta=1,
        )
        current = self.repo.get_batch(user_id="a" * 32, batch_id=batch.batch_id)
        events = self.repo.list_events(user_id="a" * 32, batch_id=batch.batch_id, after_sequence=0)
        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(current.completed_files, 1)
        self.assertEqual([event.sequence for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(sum(event.event_type == "progress" for event in events), len(events))

    def test_account_deletion_fails_active_batch_and_fences_worker(self) -> None:
        batch = self.store.batch_repository.create_batch(
            user_id="a" * 32,
            file_ids=[self.file_ids[0]],
            idempotency_key="delete-active",
        )[0]
        claim = self.store.batch_repository.claim_next(worker_id="worker", lease_seconds=300)
        assert claim is not None
        self.store.batch_repository.transition(claim, expected="pending", target="slicing")

        self.store.deactivate_user_data(user_id="a" * 32)

        current = self.store.batch_repository.get_batch(user_id="a" * 32, batch_id=batch.batch_id)
        self.assertEqual((current.status, current.error_code), ("failed", "account_deleted"))
        with self.assertRaisesRegex(RuntimeError, "stale_claim"):
            self.store.batch_repository.record_operation(
                claim,
                operation_key="slice:1",
                stage="slicing",
                ordinal=1,
                result={"intake_ids": ["ignored"]},
                completed_files_delta=1,
            )

    def test_mysql_migration_freezes_slots_snapshots_operations_and_events(self) -> None:
        migration = (
            Path(__file__).resolve().parents[1]
            / "services" / "web_domain" / "migrations" / "0012_async_intake_batches.sql"
        ).read_text(encoding="utf-8").lower()
        for table in (
            "intake_batches", "intake_batch_files", "intake_batch_operations",
            "intake_batch_events", "intake_worker_slots",
        ):
            self.assertIn(f"create table if not exists {table}", migration)
        self.assertIn("unique key uq_intake_batch_request (user_id, operation_version, idempotency_key)", migration)
        self.assertIn("check (slot_id in (1, 2))", migration)
        self.assertIn("insert ignore into intake_worker_slots", migration)
        self.assertIn("primary key (batch_id, event_sequence)", migration)


class IntakeBatchEngineTests(unittest.TestCase):
    @staticmethod
    def png_bytes() -> bytes:
        stream = BytesIO()
        Image.new("RGB", (8, 8), "white").save(stream, format="PNG")
        return stream.getvalue()

    def test_real_processor_slices_solves_grades_and_commits_only_errors(self) -> None:
        store = InMemoryNotebookStore()
        with TemporaryDirectory() as temp:
            service = NotebookService(store, Path(temp))
            verified = Question(
                "1" * 32,
                "计算下列算式：1+1=?",
                "2",
                7,
                1.0,
                "验证题库",
                version_id="2" * 32,
            )
            store.add_question(verified)
            file_id = service.upload(
                user_id="a" * 32, purpose="question_image", original_name="two.png",
                content=self.png_bytes(), idempotency_key="upload",
            ).file_id
            batch = store.batch_repository.create_batch(
                user_id="a" * 32, file_ids=[file_id], idempotency_key="batch",
            )[0]

            class Model:
                def extract(self, *, intake, file_record, image_path, thread_id=None):
                    return {
                        "intake_id": intake.intake_id, "input_version": intake.input_version,
                        "status": "complete", "thread_id": "thread-1", "route": {"model": "fake"},
                        "items": [
                            {"item_no": 1, "question_text": "计算下列算式：1+1=?", "answer_text": "3"},
                            {"item_no": 2, "question_text": "2+2=?", "answer_text": "4"},
                        ],
                    }

                def solve(self, *, attempt, image_path):
                    return {
                        "solution": "直接计算", "final_answer": "2" if attempt.answer_text == "3" else "4",
                        "verification_checks": [], "confidence": 1.0, "difficulty": "normal",
                    }

                def grade_with_solution(self, *, attempt, image_path, solution, thread_id=None, reference=None):
                    incorrect = attempt.answer_text == "3"
                    return {
                        "attempt_id": attempt.attempt_id, "input_version": attempt.input_version,
                        "verdict": "incorrect" if incorrect else "correct",
                        "first_error": "加法计算错误" if incorrect else None,
                        "cause_code": "calculation" if incorrect else None,
                        "cause_evidence": "1+1 被写成 3" if incorrect else None,
                        "knowledge_points": ["整数加法"] if incorrect else [],
                        "correct_solution": "1+1=2" if incorrect else None,
                        "final_answer": "2" if incorrect else "4",
                        "prevention_cue": "验算" if incorrect else None,
                        "confidence": 0.99,
                        "cross_validation": cross_validate_reference(reference, "9") if incorrect else None,
                        "thread_id": "thread-1", "route": {"model": "fake"},
                    }

            engine = IntakeBatchEngine(
                store.batch_repository,
                NotebookIntakeBatchProcessor(service, Model()),
                worker_id="worker",
                lease_seconds=300,
            )
            self.assertTrue(engine.run_once())
            current = store.batch_repository.get_batch(user_id="a" * 32, batch_id=batch.batch_id)
            diagnostic_events = store.batch_repository.list_events(
                user_id="a" * 32, batch_id=batch.batch_id, after_sequence=0,
            )
            self.assertEqual(
                (current.status, current.total_items, current.completed_items),
                ("completed", 2, 2),
                (current.error_code, [(item.event_type, item.data.get("error_code")) for item in diagnostic_events]),
            )
            self.assertEqual(len(store.intakes), 2)
            self.assertEqual(len(store.candidates), 2)
            self.assertEqual(len(store.errors), 1)
            linked_attempt = next(item for item in store.attempts.values() if item.question_text == "计算下列算式：1+1=?")
            self.assertEqual(linked_attempt.question_id, verified.question_id)
            events = diagnostic_events
            item_events = [event for event in events if event.event_type == "item_completed"]
            self.assertEqual(len(item_events), 2)
            self.assertEqual(item_events[0].data["item"]["notebook_status"], "saved")
            self.assertEqual(item_events[1].data["item"]["notebook_status"], "not_saved_correct")
            self.assertNotIn("candidate_id", item_events[0].data["item"])
            self.assertEqual(events[-1].event_type, "batch_completed")

    def test_grading_recovery_finishes_verified_question_link_before_receipt(self) -> None:
        store = InMemoryNotebookStore()
        with TemporaryDirectory() as temp:
            service = NotebookService(store, Path(temp))
            user_id = "a" * 32
            file_id = service.upload(
                user_id=user_id, purpose="question_image", original_name="recover.png",
                content=self.png_bytes(), idempotency_key="recover-upload",
            ).file_id
            intake, _ = store.create_intake(
                user_id=user_id, file_id=file_id, idempotency_key="recover-intake",
            )
            intake = store.save_extraction_candidate(
                user_id=user_id, intake_id=intake.intake_id,
                question_text="若 x+1=2，求 x。", answer_text="x=1", evidence={"source": "test"},
            )
            attempt_id, _ = store.confirm_intake(
                user_id=user_id, intake_id=intake.intake_id,
                expected_version=intake.input_version, idempotency_key="recover-attempt",
            )
            question = Question(
                "1" * 32, intake.question_text, "x=1", 7, 1.0, "验证题库",
                version_id="2" * 32,
            )
            store.add_question(question)
            reference = store.find_verified_question(question_text=intake.question_text)
            assert reference is not None
            validation = cross_validate_reference(reference, "x=1")
            frozen_solution = {
                "solution": "移项并验算。",
                "final_answer": "x=1",
                "verification_checks": [],
                "confidence": 1.0,
                "difficulty": "normal",
            }
            solve_sha256 = hashlib.sha256(json.dumps(
                frozen_solution, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
            ).encode("utf-8")).hexdigest()
            store.record_grade_candidate(
                user_id=user_id, attempt_id=attempt_id, input_version=1, verdict="correct",
                first_error=None,
                evidence=json.dumps({
                    "schema": "math-error-diagnosis/v1",
                    "batch_operation": {
                        "schema": "intake-grade-operation/v1",
                        "batch_id": "placeholder",
                        "operation_key": "grade:1",
                        "solve_sha256": solve_sha256,
                    },
                    "cross_validation": validation,
                }),
            )
            batch = store.batch_repository.create_batch(
                user_id=user_id, file_ids=[file_id], idempotency_key="recover-batch",
            )[0]
            candidate = next(iter(store.candidates.values()))
            evidence = json.loads(candidate.evidence)
            evidence["batch_operation"]["batch_id"] = batch.batch_id
            store.candidates[candidate.candidate_id] = type(candidate)(
                candidate.candidate_id,
                candidate.attempt_id,
                candidate.input_version,
                candidate.verdict,
                candidate.first_error,
                json.dumps(evidence),
                candidate.status,
            )
            expected_candidate_id = candidate.candidate_id
            store.record_grade_candidate(
                user_id=user_id,
                attempt_id=attempt_id,
                input_version=1,
                verdict="unclear",
                first_error=None,
                evidence=json.dumps({
                    "schema": "math-error-diagnosis/v1",
                    "batch_operation": {
                        "schema": "intake-grade-operation/v1",
                        "batch_id": "f" * 32,
                        "operation_key": "grade:1",
                        "solve_sha256": solve_sha256,
                    },
                }),
            )
            claim = store.batch_repository.claim_next(worker_id="recover-worker", lease_seconds=300)
            assert claim is not None
            store.batch_repository.transition(claim, expected="pending", target="slicing")
            store.batch_repository.record_operation(
                claim, operation_key="slice:1", stage="slicing", ordinal=1,
                result={"intake_ids": [intake.intake_id]}, completed_files_delta=1,
            )
            store.batch_repository.transition(claim, expected="slicing", target="solving", total_items=1)
            store.batch_repository.record_operation(
                claim, operation_key="solve:1", stage="solving", ordinal=1,
                result={"intake_id": intake.intake_id, "attempt_id": attempt_id, "solution": frozen_solution},
            )
            store.batch_repository.transition(claim, expected="solving", target="grading")

            class ModelMustNotRun:
                def grade_with_solution(self, **_kwargs):
                    raise AssertionError("recovery must reuse the persisted candidate")

            NotebookIntakeBatchProcessor(service, ModelMustNotRun()).process_stage(
                "grading", claim, store.batch_repository,
            )

            linked = store.get_attempt(user_id=user_id, attempt_id=attempt_id)
            self.assertEqual(linked.question_id, question.question_id)
            operation = store.batch_repository.get_operation(claim, "grade:1")
            self.assertIsNotNone(operation)
            self.assertEqual(operation.result["candidate_id"], expected_candidate_id)

    def test_solution_receipt_rejects_oversized_provider_payload(self) -> None:
        frozen = NotebookIntakeBatchProcessor._freeze_solution({
            "solution": "直接求解。",
            "final_answer": "1",
            "verification_checks": [],
            "confidence": 1.0,
            "difficulty": "normal",
            "ignored_nested_payload": {"value": "y" * 1_000_000},
        })
        self.assertNotIn("ignored_nested_payload", frozen)
        self.assertLess(len(json.dumps(frozen, ensure_ascii=False).encode("utf-8")), 32_768)
        with self.assertRaises(ModelUnavailableError):
            NotebookIntakeBatchProcessor._freeze_solution({
                "solution": "x" * 40_000,
                "final_answer": "1",
                "verification_checks": [],
                "confidence": 1.0,
                "difficulty": "normal",
            })

    def test_engine_runs_each_stage_once_and_reaches_terminal_state(self) -> None:
        clock = MutableClock()
        store = InMemoryNotebookStore()
        with TemporaryDirectory() as temp:
            service = NotebookService(store, Path(temp))
            file_id = service.upload(
                user_id="a" * 32,
                purpose="question_image",
                original_name="question.png",
                content=png_bytes(),
                idempotency_key="upload",
            ).file_id
            repo = InMemoryIntakeBatchRepository(store.get_file, clock=clock)
            batch = repo.create_batch(user_id="a" * 32, file_ids=[file_id], idempotency_key="batch")[0]
            seen: list[str] = []

            class Processor:
                def process_stage(self, stage: str, claim: BatchClaim, repository) -> int | None:
                    seen.append(stage)
                    if stage == "slicing":
                        repository.record_operation(
                            claim, operation_key="slice:1", stage=stage, ordinal=1,
                            result={"intake_ids": ["i1", "i2"]}, completed_files_delta=1,
                        )
                        return 2
                    for ordinal in (1, 2):
                        repository.record_operation(
                            claim, operation_key=f"{stage}:{ordinal}", stage=stage, ordinal=ordinal,
                            result={"ok": True}, completed_items_delta=1 if stage == "grading" else 0,
                        )
                    return None

            engine = IntakeBatchEngine(repo, Processor(), worker_id="worker", lease_seconds=300)
            self.assertTrue(engine.run_once())
            current = repo.get_batch(user_id="a" * 32, batch_id=batch.batch_id)
            self.assertEqual(seen, ["slicing", "solving", "grading"])
            self.assertEqual((current.status, current.total_items, current.completed_items), ("completed", 2, 2))
            self.assertTrue(current.terminal)
            recovered = repo.find_recoverable_batch(
                user_id="a" * 32, updated_after=clock.value - timedelta(seconds=1),
            )
            self.assertEqual(recovered.batch_id, batch.batch_id)
            self.assertFalse(engine.run_once())

    def test_heartbeat_loss_stops_stage_before_transition(self) -> None:
        clock = MutableClock()
        store = InMemoryNotebookStore()
        with TemporaryDirectory() as temp:
            service = NotebookService(store, Path(temp))
            file_id = service.upload(
                user_id="a" * 32,
                purpose="question_image",
                original_name="question.png",
                content=png_bytes(),
                idempotency_key="upload",
            ).file_id
            repo = InMemoryIntakeBatchRepository(store.get_file, clock=clock)
            batch = repo.create_batch(
                user_id="a" * 32,
                file_ids=[file_id],
                idempotency_key="heartbeat-batch",
            )[0]
            heartbeat_lost = Event()

            class Processor:
                def process_stage(self, stage: str, claim: BatchClaim, repository) -> int | None:
                    self_outer.assertEqual(stage, "slicing")
                    self_outer.assertTrue(heartbeat_lost.wait(1))
                    repository.record_operation(
                        claim,
                        operation_key="slice:1",
                        stage="slicing",
                        ordinal=1,
                        result={"intake_ids": ["i1"]},
                        completed_files_delta=1,
                    )
                    return 1

            self_outer = self
            engine = IntakeBatchEngine(repo, Processor(), worker_id="worker", lease_seconds=300)

            def fail_heartbeat(*args) -> None:
                heartbeat_lost.set()
                if len(args) == 3:
                    args[2].set()

            engine._heartbeat = fail_heartbeat
            self.assertTrue(engine.run_once())
            current = repo.get_batch(user_id="a" * 32, batch_id=batch.batch_id)
            self.assertEqual(current.status, "slicing")

    def test_third_retryable_failure_becomes_one_failed_event(self) -> None:
        clock = MutableClock()
        store = InMemoryNotebookStore()
        with TemporaryDirectory() as temp:
            service = NotebookService(store, Path(temp))
            file_id = service.upload(
                user_id="a" * 32, purpose="question_image", original_name="q.png",
                content=png_bytes(), idempotency_key="upload",
            ).file_id
            repo = InMemoryIntakeBatchRepository(store.get_file, clock=clock)
            batch = repo.create_batch(user_id="a" * 32, file_ids=[file_id], idempotency_key="batch")[0]

            class Processor:
                def process_stage(self, stage, claim, repository):
                    raise IntakeBatchFailure("model_unavailable", retryable=True)

            engine = IntakeBatchEngine(repo, Processor(), worker_id="worker", lease_seconds=300)
            for delay in (0, 5, 10):
                clock.advance(seconds=delay)
                self.assertTrue(engine.run_once())
            current = repo.get_batch(user_id="a" * 32, batch_id=batch.batch_id)
            events = repo.list_events(user_id="a" * 32, batch_id=batch.batch_id, after_sequence=0)
            self.assertEqual((current.status, current.error_code), ("failed", "model_unavailable"))
            self.assertEqual(sum(event.event_type == "batch_failed" for event in events), 1)


if __name__ == "__main__":
    unittest.main()
