from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import tempfile
import unittest

from services.web_domain import ErrorEntry, InMemoryNotebookStore, NotebookService, Question, VerifiedQuestionReference, cross_validate_reference
from services.web_domain.learning import learning_day, next_review, rank_questions
from services.web_domain.notebook import Attempt
from services.web_files import PresignedStorageRequest


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def save_bytes(self, object_key: str, content: bytes, content_type: str) -> bool:
        created = object_key not in self.objects
        self.objects.setdefault(object_key, content)
        return created

    def read_bytes(self, object_key: str) -> bytes:
        return self.objects[object_key]

    def delete_path(self, object_key: str) -> None:
        self.objects.pop(object_key, None)

    def presign_download(
        self,
        object_key: str,
        *,
        expires_in: int = 300,
        download_name: str | None = None,
    ) -> PresignedStorageRequest:
        return PresignedStorageRequest(
            method="GET",
            url=f"https://student-files.oss-cn-beijing.aliyuncs.com/{object_key}?signature=test",
            headers={},
            expires_at=datetime(2026, 8, 30, 13, 0, tzinfo=timezone.utc),
        )

    def presign_upload(self, *args, **kwargs):
        return None


class LearningLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryNotebookStore()
        self.user_id = "a" * 32
        self.error_id = "e" * 32
        self.store.errors[self.error_id] = ErrorEntry(self.error_id, self.user_id, "t" * 32, "解方程 x+1=2", "x=0", "移项符号错误", "open", datetime.now(timezone.utc))
        self.store._ensure_review(self.user_id, self.error_id)

    def test_only_verified_authorized_matching_questions_are_assigned(self) -> None:
        good = Question("1" * 32, "解方程 x+2=5", "x=3", 10, 2.0, "授权题库")
        self.store.add_question(good)
        self.store.add_question(Question("2" * 32, "证明三角形全等", "略", 10, 2.0, "授权题库"))
        self.store.add_question(Question("3" * 32, "解方程 x+3=6", "x=3", 10, 2.0, "受限题库"), license_status="restricted")
        items, gap = self.store.assign_recommendations(user_id=self.user_id, error_id=self.error_id)
        self.assertEqual([item.question.question_id for item in items], [good.question_id])
        self.assertTrue(gap)
        self.assertIn("题干共同要素", items[0].reason)
        self.store.question_rules[good.question_id] = ("retired", "open", True)
        self.assertEqual(self.store.list_recommendations(user_id=self.user_id, error_id=self.error_id), [])

    def test_daily_grade_quota_counts_unique_successes_and_keeps_a_started_batch(self) -> None:
        now = datetime(2026, 8, 29, 4, tzinfo=timezone.utc)
        first = [f"{index:032x}" for index in range(19)]
        self.store.reserve_grade_batch(user_id=self.user_id, intake_ids=first, now=now)
        for intake_id in first:
            self.store.finish_grade_usage(user_id=self.user_id, intake_id=intake_id, counted=True, now=now)
        final_batch = ["f" * 32, "e" * 32]
        self.store.reserve_grade_batch(user_id=self.user_id, intake_ids=final_batch, now=now)
        for intake_id in final_batch:
            self.store.finish_grade_usage(user_id=self.user_id, intake_id=intake_id, counted=True, now=now)
        usage = self.store.learning_usage(user_id=self.user_id, now=now)
        self.assertEqual((usage["grade"]["count"], usage["grade"]["target"], usage["grade"]["limit"]), (21, 12, 20))
        self.store.reserve_grade_batch(user_id=self.user_id, intake_ids=[first[0]], now=now)
        with self.assertRaisesRegex(RuntimeError, "daily_grade_limit"):
            self.store.reserve_grade_batch(user_id=self.user_id, intake_ids=["d" * 32], now=now)
        self.assertEqual(self.store.learning_usage(user_id=self.user_id, now=datetime(2026, 8, 29, 16, tzinfo=timezone.utc))["grade"]["count"], 0)

    def test_unclear_grade_reservation_and_recommendation_overflow_do_not_count(self) -> None:
        now = datetime.now(timezone.utc)
        self.store.reserve_grade_batch(user_id=self.user_id, intake_ids=["f" * 32], now=now)
        self.store.finish_grade_usage(user_id=self.user_id, intake_id="f" * 32, counted=False, now=now)
        self.assertEqual(self.store.learning_usage(user_id=self.user_id, now=now)["grade"]["count"], 0)
        for index in range(24):
            resource = f"{index:064x}"
            self.store.learning_usage_events[(self.user_id, learning_day(now), "recommendation", resource)] = {"kind": "recommendation", "status": "counted", "created_at": now}
        self.store.add_question(Question("9" * 32, "解方程 x+3=6", "x=3", 10, 2.0, "授权题库"))
        items, gap = self.store.assign_recommendations(user_id=self.user_id, error_id=self.error_id)
        self.assertEqual(items, [])
        self.assertTrue(gap)
        self.assertEqual(self.store.learning_usage(user_id=self.user_id, now=now)["recommendation"]["count"], 24)

    def test_review_completion_is_idempotent_and_schedules_from_completion(self) -> None:
        now = datetime(2026, 8, 23, 8, tzinfo=timezone.utc)
        task = next(iter(self.store.review_tasks.values()))
        self.store.review_tasks[task.task_id] = type(task)(task.task_id, task.user_id, task.error_id, task.stage, now, "ready")
        next_task = self.store.complete_review(user_id=self.user_id, task_id=task.task_id, result="correct", idempotency_key="review-0001", now=now)
        repeated = self.store.complete_review(user_id=self.user_id, task_id=task.task_id, result="correct", idempotency_key="review-0001", now=now)
        self.assertEqual((next_task.stage, next_task.due_at), (2, datetime(2026, 8, 24, 8, tzinfo=timezone.utc)))
        self.assertEqual(repeated.task_id, next_task.task_id)
        self.assertEqual(len(self.store.review_attempts), 1)
        progress = self.store.progress(user_id=self.user_id, now=now)
        self.assertEqual(progress["review_stage_counts"], {"1": 0, "2": 1, "3": 0, "4": 0, "5": 0, "6": 0})
        self.assertEqual((progress["today_completed_review_count"], progress["today_needs_correction_count"]), (1, 0))

    def test_partial_review_is_counted_as_today_needs_correction(self) -> None:
        now = datetime(2026, 8, 23, 8, tzinfo=timezone.utc)
        task = next(iter(self.store.review_tasks.values()))
        self.store.review_tasks[task.task_id] = type(task)(task.task_id, task.user_id, task.error_id, task.stage, now, "ready")
        next_task = self.store.complete_review(user_id=self.user_id, task_id=task.task_id, result="partial", idempotency_key="review-partial", now=now)
        progress = self.store.progress(user_id=self.user_id, now=now)
        self.assertEqual((next_task.stage, progress["review_stage_counts"]["1"]), (1, 1))
        self.assertEqual((progress["today_completed_review_count"], progress["today_needs_correction_count"]), (1, 1))

    def test_review_calendar_combines_new_due_completed_and_knowledge_events(self) -> None:
        created_at = datetime(2026, 8, 22, 20, tzinfo=timezone.utc)
        now = datetime(2026, 8, 23, 8, tzinfo=timezone.utc)
        error = self.store.errors[self.error_id]
        self.store.errors[self.error_id] = ErrorEntry(
            error.error_id, error.user_id, error.attempt_id, error.question_text, error.answer_text,
            error.first_error, error.status, created_at,
            json.dumps({"schema": "math-error-diagnosis/v1", "knowledge_points": ["一元一次方程"]}),
        )
        task = next(iter(self.store.review_tasks.values()))
        self.store.review_tasks[task.task_id] = type(task)(task.task_id, task.user_id, task.error_id, 1, now, "ready")
        self.store.complete_review(user_id=self.user_id, task_id=task.task_id, result="correct", idempotency_key="calendar-review", now=now)

        calendar = self.store.review_calendar(user_id=self.user_id, month="2026-08", now=now)

        self.assertEqual(calendar["total_error_count"], 1)
        self.assertEqual(calendar["summary"]["new_error_count"], 1)
        self.assertEqual(calendar["summary"]["due_review_count"], 2)
        self.assertEqual(calendar["summary"]["completed_review_count"], 1)
        self.assertEqual(calendar["summary"]["review_accuracy_percent"], 100)
        day = next(item for item in calendar["days"] if item["date"] == "2026-08-23")
        self.assertEqual((day["new_error_count"], day["due_review_count"], day["completed_review_count"]), (1, 1, 1))
        self.assertIn("一元一次方程", day["items"][0]["knowledge_points"])
        with self.assertRaisesRegex(ValueError, "invalid calendar month"):
            self.store.review_calendar(user_id=self.user_id, month="2026-13", now=now)

    def test_pdf_is_default_questions_only_and_user_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = NotebookService(self.store, Path(directory))
            job = service.create_practice_pdf(user_id=self.user_id, error_ids=[self.error_id], idempotency_key="practice-0001")
            filename, content = service.download_practice_pdf(user_id=self.user_id, job_id=job.job_id)
            self.assertTrue(filename.endswith(".pdf"))
            self.assertTrue(content.startswith(b"%PDF-"))
            self.assertFalse(job.checkpoint["include_answers"])
            with self.assertRaisesRegex(RuntimeError, "conflict"):
                service.create_practice_pdf(user_id=self.user_id, error_ids=[self.error_id], idempotency_key="practice-0001", include_answers=True)
            with self.assertRaises(LookupError):
                service.download_practice_pdf(user_id="b" * 32, job_id=job.job_id)

    def test_pdf_uses_injected_storage_and_presigns_only_after_ownership_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = MemoryStorage()
            service = NotebookService(self.store, Path(directory), storage=storage)
            job = service.create_practice_pdf(
                user_id=self.user_id,
                error_ids=[self.error_id],
                idempotency_key="practice-remote",
            )

            signed = service.presign_practice_pdf_download(
                user_id=self.user_id, job_id=job.job_id
            )

            self.assertIsNotNone(signed)
            assert signed is not None
            filename, request = signed
            self.assertTrue(filename.endswith(".pdf"))
            self.assertEqual(request.method, "GET")
            self.assertEqual(len(storage.objects), 1)
            with self.assertRaises(LookupError):
                service.presign_practice_pdf_download(
                    user_id="b" * 32, job_id=job.job_id
                )

    def test_review_schedule_boundaries(self) -> None:
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        self.assertEqual(next_review(2, "partial", now)[0], 2)
        self.assertEqual(next_review(4, "wrong", now)[0], 3)
        self.assertIsNone(next_review(6, "correct", now))
        self.assertEqual(rank_questions("函数 f x", [], 2), [])

    def test_verified_bank_match_is_conservative_and_conflict_blocks_commit(self) -> None:
        question = Question(
            "1" * 32, "14. 若 x+1=2，求 x。", "答案：x=1", 7, 1.0, "授权题库",
            solution_text="两边同时减一。", version_id="2" * 32, version_no=4,
        )
        self.store.add_question(question)
        self.store.add_question(Question("3" * 32, "若 x+1=2，求 x。", "x=1", 7, 1.0, "受限题库"), license_status="restricted")
        reference = self.store.find_verified_question(question_text="题干：若 x+1=2，求 x。")
        self.assertIsNotNone(reference)
        assert reference is not None
        self.assertEqual((reference.question_id, reference.version_no), (question.question_id, 4))
        self.assertEqual(cross_validate_reference(reference, "最终答案：x=1")["status"], "consistent")
        conflict = cross_validate_reference(reference, "x=2")
        attempt_id = "a" * 32
        self.store.attempts[attempt_id] = Attempt(attempt_id, self.user_id, "i" * 32, 1, question.stem_text, "x=2", "grading")
        candidate = self.store.record_grade_candidate(
            user_id=self.user_id, attempt_id=attempt_id, input_version=1, verdict="incorrect",
            first_error="计算错误", evidence=json.dumps({"schema": "math-error-diagnosis/v1", "cross_validation": conflict}),
        )
        with self.assertRaisesRegex(RuntimeError, "reference_conflict"):
            self.store.commit_grade(user_id=self.user_id, candidate_id=candidate.candidate_id, expected_version=1)
        self.assertEqual(self.store.errors, {self.error_id: self.store.errors[self.error_id]})

    def test_bank_cross_validation_canonicalizes_equivalent_structured_answers(self) -> None:
        question = Question(
            "4" * 32,
            "已知圆C经过三点，求圆的方程、弦所在直线及参数范围。",
            r"(1)$(x-2)^{2}+(y-3)^{2}=4$ (2)$3x-4y+1=0$或$x=1$； (3)$\sqrt{13}-2\le m\le \sqrt{13}+2$",
            10,
            4.0,
            "授权题库",
        )
        self.store.add_question(question)
        matched = self.store.find_verified_question(question_text=question.stem_text)
        self.assertIsNotNone(matched)
        assert matched is not None
        answer = (
            r"(1) 圆C的方程为 $(x-2)^2+(y-3)^2=4$；"
            r"(2) 直线l的方程为 $x=1$ 或 $3x-4y+1=0$；"
            r"(3) m的取值范围为 $m\in[\sqrt{13}-2,\sqrt{13}+2]$。"
        )
        validation = cross_validate_reference(matched, answer)
        self.assertEqual(validation["status"], "consistent")
        self.assertEqual(validation["reference_answer_sha256"], validation["independent_answer_sha256"])

        changed_bound = answer.replace(r"\sqrt{13}+2", r"\sqrt{13}+3")
        self.assertEqual(cross_validate_reference(matched, changed_bound)["status"], "conflict")

        escaped_reference = VerifiedQuestionReference(
            "5" * 32,
            "6" * 32,
            1,
            question.stem_text,
            r"(1)$(x-2)^{2}+(y-3)^{2}=4$\n(2)$3x-4y+1=0$或$x=1$；\n(3)$\sqrt{13}-2\le m\le \sqrt{13}+2$",
            None,
            "授权题库",
            0.99,
        )
        unicode_answer = "(1) (x-2)²+(y-3)²=4；(2) x=1 或 3x-4y+1=0；(3) √13-2≤m≤√13+2。"
        normalized = cross_validate_reference(escaped_reference, unicode_answer)
        self.assertEqual(normalized["status"], "consistent")
        self.assertEqual(normalized["reference_answer_sha256"], normalized["independent_answer_sha256"])


if __name__ == "__main__":
    unittest.main()
