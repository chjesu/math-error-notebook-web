from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import tempfile
import unittest

from services.web_domain import ErrorEntry, InMemoryNotebookStore, NotebookService, Question, cross_validate_reference
from services.web_domain.learning import next_review, rank_questions
from services.web_domain.notebook import Attempt


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

    def test_review_completion_is_idempotent_and_schedules_from_completion(self) -> None:
        now = datetime(2026, 8, 23, 8, tzinfo=timezone.utc)
        task = next(iter(self.store.review_tasks.values()))
        self.store.review_tasks[task.task_id] = type(task)(task.task_id, task.user_id, task.error_id, task.stage, now, "ready")
        next_task = self.store.complete_review(user_id=self.user_id, task_id=task.task_id, result="correct", idempotency_key="review-0001", now=now)
        repeated = self.store.complete_review(user_id=self.user_id, task_id=task.task_id, result="correct", idempotency_key="review-0001", now=now)
        self.assertEqual((next_task.stage, next_task.due_at), (2, datetime(2026, 8, 24, 8, tzinfo=timezone.utc)))
        self.assertEqual(repeated.task_id, next_task.task_id)
        self.assertEqual(len(self.store.review_attempts), 1)

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


if __name__ == "__main__":
    unittest.main()
