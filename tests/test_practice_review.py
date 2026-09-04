from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import numpy as np
from PIL import Image
from pypdf import PdfReader
import zxingcpp

from services.web_domain import ErrorEntry, GradeCandidate, InMemoryNotebookStore, NotebookService, Question
from services.web_domain.learning import Recommendation, ReviewTask, build_review_calendar
from services.web_domain.notebook import Attempt
from services.web_domain.practice_review import decode_review_qr_codes, fixed_plan_items, legacy_manifest, review_locator, shared_review_checkpoints
import test_web_app_e2e as api_tests


def qr_png(codes: list[str]) -> bytes:
    images = [Image.fromarray(np.asarray(zxingcpp.write_barcode_to_image(
        zxingcpp.create_barcode(f"LZLM1:{code}", zxingcpp.BarcodeFormat.QRCode), scale=5,
    ))) for code in codes]
    canvas = Image.new("L", (max(image.width for image in images) + 40,
                             sum(image.height for image in images) + 40 * (len(images) + 1)), "white")
    top = 40
    for image in images:
        canvas.paste(image, (20, top))
        top += image.height + 40
    buffer = BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


class PracticeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = InMemoryNotebookStore()
        self.service = NotebookService(self.store, Path(self.temp.name))
        self.owner = "a" * 32
        self.now = datetime(2026, 8, 31, 6, tzinfo=timezone.utc)
        self.error = ErrorEntry("e" * 32, self.owner, "a1", "已知一次方程 x+5=10，求未知数 x 的值。", "x=1", "移项错误", "open", self.now - timedelta(days=3))
        self.store.errors[self.error.error_id] = self.error
        self.task = ReviewTask("t" * 32, self.owner, self.error.error_id, 1, self.now - timedelta(days=2), "ready")
        self.store.review_tasks[self.task.task_id] = self.task
        self.store._review_keys[(self.owner, self.error.error_id, 1)] = self.task.task_id
        self.question = Question("b" * 32, "已知一元二次方程 y 的平方等于九，求 y 的全部实数解。", "y=3或-3", 10, 2, "测试题库")
        self.store.add_question(self.question)
        self.store.recommendations["r1"] = Recommendation("r1", self.owner, self.error.error_id, self.question, "同知识点", "assigned")

    def paper(self, *, key="paper", legacy=False, plan_kind="daily_review"):
        job = self.service.create_practice_pdf(user_id=self.owner, error_ids=[self.error.error_id], idempotency_key=key, plan_kind=plan_kind)
        if legacy:
            checkpoint = dict(job.checkpoint)
            checkpoint.pop("review_manifest")
            checkpoint.pop("review_job_id")
            self.store.jobs[job.job_id] = replace(job, checkpoint=checkpoint)
            job = self.store.jobs[job.job_id]
        return job

    def candidate(self, item, *, verdict="correct", job=None, context=None):
        job = job or self.job
        if context is None:
            context = self.service.resolve_practice_review(user_id=self.owner, question_text=item["stem_text"], locator={"code": item["code"]})
        self.assertIsNotNone(context)
        return GradeCandidate(uuid.uuid4().hex, "attempt", 1, verdict, None, json.dumps({"schema": "math-error-diagnosis/v1", "practice_review": context, "knowledge_points": ["方程"], "cause_evidence": "测试错因"}), "candidate")

    def submit(self, index, verdict="correct", *, now=None):
        return self.service.commit_practice_review(user_id=self.owner, candidate=self.candidate(self.job.checkpoint["review_manifest"][index], verdict=verdict), now=now or self.now)

    def test_cross_day_split_completion_and_replay(self):
        self.job = self.paper()
        first = self.submit(0)
        self.assertEqual((first["status"], first["completed_question_count"], len(self.store.review_attempts)), ("review_waiting", 1, 0))
        second = self.submit(1)
        self.assertEqual(second["status"], "review_completed")
        self.assertEqual(second["completed_at"], self.now.isoformat())
        self.assertEqual(datetime.fromisoformat(second["next_due_at"]), self.now + timedelta(days=1))
        self.assertEqual(second["next_stage"], 2)
        replay = self.submit(0, now=self.now + timedelta(days=2))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["completed_at"], second["completed_at"])
        self.assertEqual((len(self.store.review_attempts), len(self.store.errors)), (1, 1))

    def test_reprints_share_partial_submissions_and_complete_once(self):
        original = self.paper(key="original")
        reprint = self.paper(key="reprint", plan_kind="practice")
        self.job = original
        self.assertEqual(self.submit(1)["completed_question_count"], 1)
        papers = self.service.list_practice_pdfs(user_id=self.owner)
        self.assertEqual([paper["progress"]["answered_count"] for paper in papers], [1, 1])
        self.assertNotIn("_checkpoint", papers[0])
        self.job = reprint
        self.assertEqual(self.submit(0)["status"], "review_completed")
        self.job = original
        replay = self.submit(1, now=self.now + timedelta(days=1))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["completed_at"], self.now.isoformat())
        self.assertEqual(len(self.store.review_attempts), 1)
        self.assertEqual([paper["progress"]["answered_count"] for paper in self.service.list_practice_pdfs(user_id=self.owner)], [2, 2])

    def test_review_code_state_names_inherited_result_and_next_pending_item(self):
        original = self.paper(key="original")
        reprint = self.paper(key="reprint", plan_kind="practice")
        self.job = original
        self.submit(1)
        state = self.service.inspect_review_code(
            user_id=self.owner, code=reprint.checkpoint["review_manifest"][1]["code"]
        )
        self.assertEqual((state["status"], state["answered_count"], state["required_count"]), ("correct", 1, 2))
        self.assertEqual(state["inherited_from_code"], original.checkpoint["review_manifest"][1]["code"])
        self.assertEqual(state["recommended_action"], "submit_remaining_required")
        self.assertEqual(state["pending_items"][0]["review_code"], reprint.checkpoint["review_manifest"][0]["code"])

    def test_explicit_correction_replaces_unfinished_submission_and_keeps_audit_chain(self):
        self.job = self.paper()
        item = self.job.checkpoint["review_manifest"][1]
        first = self.candidate(item, verdict="partial")
        self.service.commit_practice_review(user_id=self.owner, candidate=first, now=self.now)
        context = self.service.resolve_practice_review(
            user_id=self.owner, question_text=item["stem_text"], locator={"code": item["code"]}
        ) | {"correction": True}
        corrected = self.candidate(item, verdict="correct", context=context)
        receipt = self.service.commit_practice_review(user_id=self.owner, candidate=corrected, now=self.now + timedelta(hours=1))
        saved = self.store.jobs[self.job.job_id].checkpoint["review_submissions"][item["code"]]
        self.assertEqual((receipt["status"], saved["verdict"], saved["revision"]), ("review_waiting", "correct", 1))
        self.assertEqual((saved["previous_candidate_id"], saved["previous_verdict"]), (first.candidate_id, "partial"))
        self.assertEqual(saved["history"], [{
            "candidate_id": first.candidate_id, "verdict": "partial", "submitted_at": self.now.isoformat(),
        }])

    def test_calendar_keeps_paper_progress_and_actual_submission_day_separate(self):
        original = self.paper(key="original")
        reprint = self.paper(key="reprint", plan_kind="practice")
        for job in (original, reprint):
            self.store.jobs[job.job_id] = replace(job, checkpoint=job.checkpoint | {"generated_at": "2026-08-28T23:00:00+00:00"})
        self.job = original
        # UTC August 30 evening is August 31 in China.
        submitted = datetime(2026, 8, 30, 16, 30, tzinfo=timezone.utc)
        self.submit(1, now=submitted)
        self.job = reprint
        self.submit(1, now=self.now)
        calendar = self.service.review_calendar(user_id=self.owner, month="2026-08", now=self.now)
        days = {day["date"]: day for day in calendar["days"]}
        self.assertEqual((days["2026-08-29"]["paper_answered_count"], days["2026-08-29"]["paper_required_count"]), (1, 2))
        self.assertEqual(len(days["2026-08-29"]["practice_plans"]), 1)
        self.assertEqual(days["2026-08-29"]["submitted_question_count"], 0)
        self.assertEqual(days["2026-08-30"]["submitted_question_count"], 0)
        self.assertEqual(days["2026-08-31"]["submitted_question_count"], 1)
        self.assertEqual(days["2026-08-31"]["practice_activity"][0]["submitted_at"], submitted.isoformat())
        self.assertEqual(calendar["summary"]["submitted_question_count"], 1)
        self.assertEqual(calendar["summary"]["completed_review_count"], 0)
        other = self.service.review_calendar(user_id="other", month="2026-08", now=self.now)
        self.assertTrue(all(not day["practice_plans"] and not day["practice_activity"] for day in other["days"]))

    def test_settled_correction_clears_current_counts_without_rewriting_history(self):
        self.job = self.paper()
        reprint = self.paper(key="reprint", plan_kind="practice")
        original = self.candidate(self.job.checkpoint["review_manifest"][0], verdict="incorrect")
        self.service.commit_practice_review(user_id=self.owner, candidate=original, now=self.now)
        first_receipt = self.submit(1)
        before = deepcopy((self.store.review_attempts, self.store.review_tasks, self.store.errors))
        self.assertEqual(self.service.progress(user_id=self.owner, now=self.now)["today_needs_correction_count"], 1)
        item = reprint.checkpoint["review_manifest"][0]
        context = self.service.resolve_practice_review(user_id=self.owner, question_text=item["stem_text"],
            locator={"code": item["code"]}) | {"correction": True}
        correction = self.candidate(item, context=context)
        later = self.now + timedelta(days=1)
        with patch.object(self.store, "complete_review", side_effect=AssertionError("corrections must not reschedule")):
            receipt = self.service.commit_practice_review(user_id=self.owner, candidate=correction, now=later)
            replay = self.service.commit_practice_review(user_id=self.owner, candidate=correction, now=later)
            # A retry of a superseded grade must not revert the accepted correction.
            stale = replace(original, evidence=json.dumps({"practice_review": context}))
            self.service.commit_practice_review(user_id=self.owner, candidate=stale, now=later)
        self.assertEqual((receipt["status"], replay["status"], receipt["review_result"]),
                         ("review_corrected", "review_corrected", "wrong"))
        self.assertTrue(replay["replayed"])
        self.assertEqual(before, (self.store.review_attempts, self.store.review_tasks, self.store.errors))
        checkpoint = self.store.jobs[reprint.job_id].checkpoint
        self.assertEqual(checkpoint["review_receipts"][self.task.task_id], first_receipt)
        saved = checkpoint["review_submissions"][item["code"]]
        self.assertEqual((saved["revision"], saved["verdict"], saved["submitted_at"]), (1, "correct", self.now.isoformat()))
        self.assertEqual(saved["history"][0]["candidate_id"], original.candidate_id)
        calendar = self.service.review_calendar(user_id=self.owner, month="2026-08", now=later)
        self.assertEqual((calendar["summary"]["needs_correction_count"], calendar["summary"]["completed_review_count"],
                          calendar["summary"]["review_accuracy_percent"], calendar["summary"]["submitted_question_count"]), (0, 1, 0, 2))
        self.assertEqual(self.service.progress(user_id=self.owner, now=self.now)["today_needs_correction_count"], 0)
        json.dumps(calendar)  # API response remains JSON serializable.
        papers = self.service.list_practice_pdfs(user_id=self.owner)
        self.assertEqual([paper["progress"]["needs_correction_count"] for paper in papers], [0, 0])
        plan = self.service.today_practice_plan(user_id=self.owner, papers=papers,
            now=datetime.fromisoformat(self.job.checkpoint["generated_at"]))
        self.assertEqual((plan["items"][0]["status"], plan["items"][0]["result"]), ("completed", "wrong"))

    def test_settled_partial_correction_remains_outstanding_and_unclear_does_not_replace(self):
        self.job = self.paper()
        self.submit(0, "incorrect")
        self.submit(1, "partial")
        item = self.job.checkpoint["review_manifest"][0]
        context = self.service.resolve_practice_review(user_id=self.owner, question_text=item["stem_text"],
            locator={"code": item["code"]}) | {"correction": True}
        candidate = self.candidate(item, context=context)
        self.assertEqual(self.service.commit_practice_review(user_id=self.owner, candidate=candidate, now=self.now)["status"], "review_needs_correction")
        self.assertEqual(self.service.progress(user_id=self.owner, now=self.now)["today_needs_correction_count"], 1)
        before = deepcopy(self.store.jobs[self.job.job_id].checkpoint)
        unclear = self.candidate(item, verdict="unclear", context=context)
        self.assertEqual(self.service.commit_practice_review(user_id=self.owner, candidate=unclear, now=self.now)["status"], "needs_review")
        self.assertEqual(before, self.store.jobs[self.job.job_id].checkpoint)
        self.store.set_error_status(user_id=self.owner, error_id=self.error.error_id, status="removed")
        self.assertEqual(self.service.commit_practice_review(user_id=self.owner, candidate=candidate, now=self.now)["status"], "review_stale")

    def test_non_pdf_later_review_resolves_old_correction_without_erasing_grade(self):
        attempts = [{"error_id": "e", "task_id": "t", "stage": 1, "result": "wrong", "completed_at": self.now},
                    {"error_id": "e", "task_id": "t", "stage": 1, "result": "correct", "completed_at": self.now + timedelta(days=1)}]
        calendar = build_review_calendar("2026-08", errors=[], review_tasks=[], review_attempts=attempts,
                                         total_error_count=1, now=self.now + timedelta(days=1))
        self.assertEqual((calendar["summary"]["needs_correction_count"], calendar["summary"]["completed_review_count"]), (0, 1))
        self.assertEqual(calendar["days"][-1]["items"][0]["result"], "wrong")

    def test_deferred_pdf_correction_is_hidden_until_resumed(self):
        self.job = self.paper()
        self.submit(0, "incorrect")
        task = self.store.list_active_reviews(user_id=self.owner)[0]
        self.assertEqual(self.service.progress(user_id=self.owner, now=self.now)["today_needs_correction_count"], 1)
        self.store.defer_review(user_id=self.owner, task_id=task.task_id, days=3,
            reason="prerequisite_not_learned", idempotency_key="defer-pdf", now=self.now)
        self.assertEqual(self.service.progress(user_id=self.owner, now=self.now)["today_needs_correction_count"], 0)
        self.store.resume_review(user_id=self.owner, task_id=task.task_id, idempotency_key="resume-pdf", now=self.now)
        self.assertEqual(self.service.progress(user_id=self.owner, now=self.now)["today_needs_correction_count"], 1)

    def test_reprint_state_does_not_leak_to_new_round_or_other_account(self):
        original = self.paper()
        self.job = original
        self.submit(1)
        old_checkpoint = self.store.jobs[original.job_id].checkpoint
        changed = deepcopy(original.checkpoint)
        for row in changed["review_manifest"]:
            row["due_at"] = (self.task.due_at + timedelta(days=1)).isoformat()
        shared = shared_review_checkpoints({"old": old_checkpoint, "new": changed})
        self.assertFalse(shared["new"].get("review_submissions"))
        changed = deepcopy(original.checkpoint)
        changed["review_manifest"][1]["stem_text"] += " 条件改为十一。"
        self.assertFalse(shared_review_checkpoints({"old": old_checkpoint, "changed": changed})["changed"].get("review_submissions"))
        self.assertEqual(self.service.list_practice_pdfs(user_id="other"), [])

    def test_reprint_failure_rolls_back_without_losing_other_paper_submission(self):
        original = self.paper(key="original")
        reprint = self.paper(key="reprint", plan_kind="practice")
        self.job = original
        self.submit(1)
        before = deepcopy(self.store.jobs)
        self.job = reprint
        with patch.object(self.store, "complete_review", side_effect=RuntimeError("simulated storage failure")):
            with self.assertRaises(RuntimeError):
                self.submit(0)
        self.assertEqual(before, self.store.jobs)
        self.assertFalse(self.store.review_attempts)
        self.assertEqual(self.submit(0)["status"], "review_completed")

    def test_reprints_with_different_required_items_do_not_share_completion_receipt(self):
        self.job = self.paper()
        self.submit(0)
        self.submit(1)
        completed = self.store.jobs[self.job.job_id].checkpoint
        shorter = deepcopy(self.job.checkpoint)
        shorter["review_manifest"] = shorter["review_manifest"][:1]
        shared = shared_review_checkpoints({"full": completed, "short": shorter})
        self.assertFalse(shared["short"].get("review_receipts"))

    def test_unfrozen_legacy_pdf_does_not_claim_zero_completed(self):
        self.paper(legacy=True)
        paper = self.service.list_practice_pdfs(user_id=self.owner)[0]
        self.assertFalse(paper["progress"]["available"])

    def test_today_plan_stays_fixed_when_same_task_finishes_from_older_pdf(self):
        older = self.paper(key="older")
        self.store.jobs[older.job_id] = replace(older, checkpoint=older.checkpoint | {"generated_at": "2026-08-29T00:00:00+00:00"})
        today = self.paper(key="today")
        self.service.create_practice_pdf(user_id=self.owner, error_ids=[self.error.error_id], idempotency_key="extra-practice", plan_kind="practice")
        papers = self.store.list_practice_pdfs(user_id=self.owner)
        initial = self.service.today_practice_plan(user_id=self.owner, papers=papers,
                                                   now=datetime.fromisoformat(today.checkpoint["generated_at"]))
        self.assertEqual((initial["task_id"], initial["items"][0]["status"]), (today.job_id, "pending"))
        self.job = older
        self.submit(0)
        self.submit(1)
        updated = self.service.today_practice_plan(user_id=self.owner, papers=self.store.list_practice_pdfs(user_id=self.owner),
                                                   now=datetime.fromisoformat(today.checkpoint["generated_at"]))
        self.assertEqual(updated["task_id"], today.job_id)
        self.assertEqual(updated["items"], [{"error_id": self.error.error_id, "stage": 1, "status": "completed",
                                             "result": "correct", "completed_at": self.now.isoformat()}])
        self.assertEqual(self.store.list_due_reviews(user_id=self.owner, now=self.now), [])

    def test_frozen_plan_reports_correction_without_backfilling(self):
        manifest = [{"error_id": self.error.error_id, "task_id": self.task.task_id, "stage": 1, "due_at": self.task.due_at.isoformat()}]
        rows = fixed_plan_items(manifest, [{"task_id": self.task.task_id, "stage": 1, "result": "wrong", "completed_at": self.now}], [])
        self.assertEqual(rows, [{"error_id": self.error.error_id, "stage": 1, "status": "needs_correction",
                                 "result": "wrong", "completed_at": self.now.isoformat()}])

    def test_partial_and_wrong_aggregate_and_never_duplicate(self):
        for verdict, result in [("incorrect", "wrong"), ("partial", "partial")]:
            with self.subTest(verdict=verdict):
                self.setUp()
                self.job = self.paper()
                self.submit(0, verdict)
                receipt = self.submit(1)
                self.assertEqual((receipt["status"], receipt["review_result"], receipt["next_stage"]), ("review_needs_correction", result, 1))
                self.assertEqual(len(self.store.errors), 1)

    def test_reference_only_and_final_stage_mastery(self):
        self.store.review_tasks[self.task.task_id] = replace(self.task, stage=6)
        self.job = self.paper()
        self.assertFalse(self.job.checkpoint["review_manifest"][0]["required"])
        self.assertEqual(self.submit(0)["status"], "review_reference_only")
        result = self.submit(1)
        self.assertEqual((result["status"], result["next_stage"]), ("review_completed", None))
        self.assertEqual(self.store.errors[self.error.error_id].status, "mastered")

    def test_no_recommendations_falls_back_to_required_original(self):
        self.store.recommendations.clear()
        self.store.review_tasks[self.task.task_id] = replace(self.task, stage=3)
        self.job = self.paper()
        self.assertTrue(self.job.checkpoint["review_manifest"][0]["required"])
        self.assertEqual(self.submit(0)["status"], "review_completed")

    def test_recycled_task_due_time_and_removed_error_are_protected(self):
        self.job = self.paper()
        self.store.review_tasks[self.task.task_id] = replace(self.task, due_at=self.task.due_at + timedelta(days=1))
        self.assertEqual(self.submit(0)["status"], "review_stale")
        self.store.review_tasks[self.task.task_id] = self.task
        self.store.set_error_status(user_id=self.owner, error_id=self.error.error_id, status="removed")
        self.assertEqual(self.submit(0)["status"], "review_stale")
        self.assertFalse(self.store.review_attempts)
        progress = self.service.list_practice_pdfs(user_id=self.owner)[0]["progress"]
        self.assertEqual((progress["required_count"], progress["needs_correction_count"], progress["items"]), (0, 0, []))

    def test_early_or_unclear_never_advances(self):
        self.store.review_tasks[self.task.task_id] = replace(self.task, due_at=self.now + timedelta(days=1))
        self.job = self.paper()
        self.assertEqual(self.submit(0, "unclear")["status"], "needs_review")
        self.assertFalse(self.store.jobs[self.job.job_id].checkpoint.get("review_submissions"))
        self.submit(0)
        self.assertEqual(self.submit(1)["status"], "review_waiting")
        self.assertFalse(self.store.review_attempts)
        self.assertEqual(self.submit(1, now=self.now + timedelta(days=1))["status"], "review_completed")

    def test_cross_account_and_wrong_code_dont_match(self):
        self.job = self.paper()
        item = self.job.checkpoint["review_manifest"][0]
        result = self.service.resolve_practice_review(user_id="other", question_text=item["stem_text"], locator={"code": item["code"]})
        self.assertEqual(result["status"], "unmatched")
        candidate = self.candidate(item)
        result = self.service.commit_practice_review(user_id="other", candidate=candidate)
        self.assertEqual(result["status"], "review_unmatched")
        wrong = item["code"][:-1] + ("0" if item["code"][-1] != "0" else "1")
        result = self.service.resolve_practice_review(user_id=self.owner, question_text=self.question.stem_text, locator={"code": wrong})
        self.assertEqual(result["status"], "unmatched")

    def test_checked_code_and_unique_question_id_do_not_depend_on_ocr_similarity(self):
        self.job = self.paper()
        original, recommendation = self.job.checkpoint["review_manifest"]
        self.assertRegex(original["code"], r"^R[0-9a-f]{12}-01-[0-9A-F]{6}$")
        by_code = self.service.resolve_practice_review(
            user_id=self.owner, question_text="OCR 完全无法还原题干", locator={"code": original["code"]}
        )
        by_question = self.service.resolve_practice_review(
            user_id=self.owner, question_text=r"错误的 LaTeX 与额外数字 2026 99",
            locator={"question_id": self.question.question_id, "kind": "recommendation"},
        )
        self.assertEqual((by_code["code"], by_question["code"]), (original["code"], recommendation["code"]))
        self.assertEqual(
            (by_question["question_id"], by_question["stem_text"], by_question["error_id"], by_question["kind"]),
            (self.question.question_id, recommendation["stem_text"], self.error.error_id, "recommendation"),
        )

    def test_pending_options_keep_exact_code_and_bound_semantic_fallback(self):
        papers = [self.paper(key=f"paper-{index}", plan_kind="practice") for index in range(10)]
        exact = papers[4].checkpoint["review_manifest"][0]

        def pending(locator, suffix):
            attempt_id = suffix * 32
            self.store.attempts[attempt_id] = Attempt(
                attempt_id, self.owner, (suffix.upper() * 32)[:32], 1,
                "OCR 严重损坏：完全不同的短文本 987654321", "x=5", "grade_ready",
            )
            return self.store.record_grade_candidate(
                user_id=self.owner, attempt_id=attempt_id, input_version=1, verdict="correct",
                first_error=None, evidence=json.dumps({
                    "schema": "math-error-diagnosis/v1",
                    "practice_review": {"status": "unmatched", "locator": locator},
                }, ensure_ascii=False),
            )

        exact_candidate = pending({"code": exact["code"]}, "1")
        exact_options = next(item["options"] for item in self.service.list_pending_practice_review_links(
            user_id=self.owner) if item["candidate_id"] == exact_candidate.candidate_id)
        self.assertEqual([item["code"] for item in exact_options], [exact["code"]])
        self.assertEqual(exact_options[0]["candidate_source"], "visible_identity")

        fallback = pending({}, "2")
        fallback_options = next(item["options"] for item in self.service.list_pending_practice_review_links(
            user_id=self.owner) if item["candidate_id"] == fallback.candidate_id)
        self.assertEqual(len(fallback_options), 8)
        self.assertTrue(all(item["candidate_source"] == "semantic_candidate" for item in fallback_options))

    def test_pending_review_can_be_selected_once_without_creating_a_new_error(self):
        self.job = self.paper()
        attempt_id = "9" * 32
        intake_id = "8" * 32
        self.store.attempts[attempt_id] = Attempt(
            attempt_id, self.owner, intake_id, 1, self.question.stem_text, "y=3或-3", "grade_ready"
        )
        evidence = json.dumps({
            "schema": "math-error-diagnosis/v1", "knowledge_points": ["方程"],
            "practice_review": {"status": "unmatched", "locator": {"kind": "recommendation"}, "correction": True},
        }, ensure_ascii=False)
        candidate = self.store.record_grade_candidate(
            user_id=self.owner, attempt_id=attempt_id, input_version=1, verdict="correct",
            first_error=None, evidence=evidence,
        )
        pending = self.service.list_pending_practice_review_links(user_id=self.owner)
        self.assertEqual((len(pending), pending[0]["candidate_id"], len(pending[0]["options"])), (1, candidate.candidate_id, 1))
        linked = self.service.link_pending_practice_review(
            user_id=self.owner, candidate_id=candidate.candidate_id, input_version=1,
            code=pending[0]["options"][0]["code"],
        )
        context = json.loads(linked.evidence)["practice_review"]
        self.assertEqual((context["status"], context["error_id"], context["stage"]),
                         ("matched", self.error.error_id, self.task.stage))
        self.assertTrue(context["correction"])
        self.assertEqual(self.service.list_pending_practice_review_links(user_id=self.owner), [])
        self.assertEqual(len(self.store.errors), 1)

    def test_transaction_failure_rolls_back_completion_and_submissions(self):
        self.job = self.paper()
        self.submit(0)
        old = deepcopy(self.store.jobs[self.job.job_id].checkpoint)
        real = self.store.complete_review
        def fail(**kwargs):
            real(**kwargs)
            raise RuntimeError("simulated storage failure")
        with patch.object(self.store, "complete_review", side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.submit(1)
        self.assertEqual(self.store.jobs[self.job.job_id].checkpoint, old)
        self.assertFalse(self.store.review_attempts)
        self.assertEqual(self.store.review_tasks[self.task.task_id], self.task)
        self.assertEqual(self.submit(1)["status"], "review_completed")

    def test_automatic_pending_association_keeps_correction_intent(self):
        self.job = self.paper()
        item = self.job.checkpoint["review_manifest"][1]
        self.store.attempts["9" * 32] = Attempt(
            "9" * 32, self.owner, "8" * 32, 1, item["stem_text"], "y=3或-3", "grade_ready"
        )
        candidate = self.store.record_grade_candidate(user_id=self.owner, attempt_id="9" * 32, input_version=1,
            verdict="correct", first_error=None, evidence=json.dumps({"practice_review": {
                "status": "unmatched", "locator": {"code": item["code"]}, "correction": True,
            }}))
        prepared = self.service.prepare_review_candidate(user_id=self.owner, candidate=candidate)
        context = json.loads(prepared.evidence)["practice_review"]
        self.assertEqual((context["status"], context["code"], context["correction"]), ("matched", item["code"], True))

    def test_old_pdf_is_read_and_matched_without_changing_generation_date(self):
        self.job = self.paper(legacy=True)
        created = self.job.checkpoint["generated_at"]
        legacy_page = type("LegacyPage", (), {"extract_text": lambda _self:
            f"错题编号 {self.error.error_id[:8]}（第 1 阶段 · 需重做）\n题库编号 {self.question.question_id}"})()
        with patch("pypdf.PdfReader") as reader:
            reader.return_value.pages = [legacy_page]
            context = self.service.resolve_practice_review(user_id=self.owner, question_text=self.error.question_text,
                locator={"pdf_id": self.job.job_id, "error_id": self.error.error_id[:8], "stage": 1, "kind": "original"})
        self.assertEqual(context["status"], "matched")
        self.assertEqual(self.store.jobs[self.job.job_id].checkpoint["generated_at"], created)
        self.assertEqual(len(self.store.jobs[self.job.job_id].checkpoint["review_manifest"]), 2)

    def test_legacy_paper_cannot_attach_to_recycled_stage_or_deleted_history(self):
        self.job = self.paper(legacy=True)
        generated = datetime.fromisoformat(self.job.checkpoint["generated_at"])
        self.store.review_tasks[self.task.task_id] = replace(self.task, due_at=generated + timedelta(days=1))
        context = self.service.resolve_practice_review(user_id=self.owner, question_text=self.error.question_text, review_mode=True)
        self.assertEqual(context["status"], "unmatched")
        del self.store.jobs[self.job.job_id]
        self.assertEqual(self.service.resolve_practice_review(user_id=self.owner, question_text=self.error.question_text, review_mode=True)["status"], "unmatched")

    def test_pdf_hides_machine_identifiers_and_legacy_skill_identifiers_map(self):
        self.job = self.paper()
        _, content = self.service.download_practice_pdf(user_id=self.owner, job_id=self.job.job_id)
        text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(content)).pages)
        for item in self.job.checkpoint["review_manifest"]:
            self.assertNotIn(item["code"], text)
        self.assertNotIn("复习码", text)
        self.assertNotIn("错题编号", text)
        self.assertNotIn("题库编号", text)
        source = "ERR-20260829-abcdef12"
        migrated = hashlib.sha256(f"desktop-error:error:{self.owner}:{source}".encode()).hexdigest()[:32]
        error = replace(self.error, error_id=migrated)
        task = replace(self.task, error_id=migrated)
        result = legacy_manifest(f"错题编号 {source}（第 1 阶段 · 需重做）", self.job.job_id, [error], [task], {}, self.now)
        self.assertEqual(result[0]["error_id"], migrated)

    def test_checked_code_ignores_conflicting_model_ocr_metadata(self):
        self.job = self.paper()
        item = self.job.checkpoint["review_manifest"][1]
        context = self.service.resolve_practice_review(
            user_id=self.owner,
            question_text="模型还把题干抄错了",
            locator={
                "code": item["code"], "pdf_id": "wrong.pdf", "error_id": "wrong",
                "question_id": "wrong", "stage": 6, "kind": "original",
            },
            review_mode=True,
        )
        self.assertEqual(
            (context["status"], context["job_id"], context["error_id"], context["question_id"],
             context["stage"], context["kind"], context["stem_text"]),
            ("matched", self.job.job_id, item["error_id"], item["question_id"],
             item["stage"], item["kind"], item["stem_text"]),
        )

    def test_review_qr_decoder_accepts_only_product_codes_in_page_order(self):
        self.job = self.paper()
        codes = [item["code"] for item in self.job.checkpoint["review_manifest"]]
        self.assertEqual(decode_review_qr_codes(qr_png(codes)), [code.upper() for code in codes])
        foreign = zxingcpp.write_barcode_to_image(
            zxingcpp.create_barcode("https://example.com", zxingcpp.BarcodeFormat.QRCode), scale=5,
        )
        buffer = BytesIO()
        Image.fromarray(np.asarray(foreign)).save(buffer, format="PNG")
        self.assertEqual(decode_review_qr_codes(buffer.getvalue()), [])

    def test_locators_are_bounded_and_normal_questions_stay_new(self):
        for locator in [{"user_id": "other"}, {"stage": True}, {"stage": 7}, {"code": "x" * 181}]:
            with self.assertRaises(ValueError):
                review_locator(locator)
        self.assertIsNone(self.service.resolve_practice_review(user_id=self.owner, question_text=self.error.question_text))

    def test_diagram_paths_do_not_prevent_match_or_bank_cross_check(self):
        decorated = replace(self.question, stem_text=self.question.stem_text + " ![原题图](bank-assets/" + "1" * 64 + ".png)")
        self.store.add_question(decorated)
        self.store.recommendations["r1"] = replace(self.store.recommendations["r1"], question=decorated)
        self.job = self.paper()
        context = self.service.resolve_practice_review(user_id=self.owner, question_text=self.question.stem_text,
            locator={"code": self.job.checkpoint["review_manifest"][1]["code"]})
        self.assertEqual(context["status"], "matched")
        self.assertIsNotNone(self.store.find_verified_question(question_text=self.question.stem_text))

    def test_old_answer_appendix_is_not_counted_as_more_required_questions(self):
        text = f"错题编号 {self.error.error_id[:8]}（第 1 阶段 · 需重做）\n题库编号 {self.question.question_id}\n答案\n题库编号 {self.question.question_id}\n"
        items = legacy_manifest(text, "f" * 32, [self.error], [self.task], {self.error.error_id: list(self.store.recommendations.values())}, self.now)
        self.assertEqual(len(items), 2)


class PracticeReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.client = api_tests.NotebookE2ETests()
        self.client.setUp()
        self.addCleanup(self.client.tearDown)
        self.fixture = PracticeReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client.app.notebook = self.fixture.service
        self.fixture.store.bind_model_session(user_id=self.fixture.owner, session_id="review-test")
        self.job = self.fixture.paper()

    def process(self, index=0, *, code=True, conflict=False, color="white", question_text=None,
                image_review_code=None, review=None):
        item = self.job.checkpoint["review_manifest"][index]
        if image_review_code:
            content = qr_png([image_review_code])
        else:
            buffer = BytesIO()
            Image.new("RGB", (12, 12), color).save(buffer, format="PNG")
            content = buffer.getvalue()
        row = {"item_no": 1, "question_text": question_text or item["stem_text"], "answer_text": "x=5", "verdict": "incorrect" if conflict else "correct",
            "first_error": "计算错误" if conflict else "", "cause_code": "calculation" if conflict else "", "cause_evidence": "计算时漏掉一步" if conflict else "",
            "knowledge_points": ["方程"], "correct_solution": "移项并求解得到正确答案", "final_answer": "y=100" if conflict else "y=3或-3", "prevention_cue": "检查计算", "confidence": 0.98,
            "review": review or {"code": item["code"] if code else "invalid-code"}}
        payload = {"session_id": "review-test", "review_mode": True, "attachment": {"attachment_id": "sha256:" + hashlib.sha256(content).hexdigest(), "name": "review.png", "media_type": "image/png", "data": base64.b64encode(content).decode()}, "items": [row]}
        self.last_payload = payload
        result = self.call("/v1/internal/harness/intakes/process", payload)
        self.assertEqual(result[0], 200, result)
        return result[2]["results"][0]

    def test_checked_review_code_uses_frozen_stem_and_exact_question_reference(self):
        item = self.job.checkpoint["review_manifest"][1]
        result = self.process(1, color="green", question_text=r"已知 y^2/3=9，求 y。")
        self.assertEqual(result["question_text"], item["stem_text"])
        self.assertEqual(result["review_association"], {
            "status": "matched", "pdf_id": self.job.job_id, "review_code": item["code"],
            "error_id": item["error_id"], "question_id": item["question_id"], "stage": item["stage"],
            "kind": item["kind"],
        })
        candidate = self.fixture.store.get_grade_candidate(user_id=self.fixture.owner, candidate_id=result["candidate_id"])
        attempt = self.fixture.store.get_attempt(user_id=self.fixture.owner, attempt_id=candidate.attempt_id)
        self.assertEqual((attempt.question_text, attempt.question_id), (item["stem_text"], self.fixture.question.question_id))

    def test_qr_code_overrides_model_ocr_identity(self):
        item = self.job.checkpoint["review_manifest"][1]
        result = self.process(1, image_review_code=item["code"], review={
            "code": "invalid-code", "pdf_id": "wrong.pdf", "error_id": "wrong",
            "question_id": "wrong", "stage": 6, "kind": "original",
        })
        self.assertEqual(result["review_association"], {
            "status": "matched", "pdf_id": self.job.job_id, "review_code": item["code"],
            "error_id": item["error_id"], "question_id": item["question_id"],
            "stage": item["stage"], "kind": item["kind"],
        })

    def call(self, path, payload):
        return self.client.call(path, method="POST", payload=payload, origin=None,
            client=("127.0.0.1", 10001), extra_headers={"authorization": "Bearer test-internal-token"})

    def test_internal_process_waits_then_advances_without_new_errors(self):
        self.assertEqual(self.process()["receipt_status"], "review_waiting")
        result = self.process(1, color="blue")
        self.assertEqual(result["receipt_status"], "review_completed")
        self.assertEqual(len(self.fixture.store.errors), 1)
        self.assertEqual(len(self.fixture.store.review_attempts), 1)
        candidate = self.fixture.store.get_grade_candidate(user_id=self.fixture.owner, candidate_id=result["candidate_id"])
        self.assertIn("方程", json.loads(candidate.evidence)["knowledge_points"])

    def test_one_photo_with_all_required_items_returns_current_group_receipts(self):
        with patch.object(self.client, "call", return_value=(200, {}, {"results": [{}]})):
            self.process()
        payload = self.last_payload
        row = dict(payload["items"][0])
        item = self.job.checkpoint["review_manifest"][1]
        row.update(item_no=2, question_text=item["stem_text"], review={"code": item["code"]})
        payload["items"].append(row)
        response = self.call("/v1/internal/harness/intakes/process", payload)
        self.assertEqual(response[0], 200, response)
        self.assertEqual([item["receipt_status"] for item in response[2]["results"]], ["review_completed", "review_completed"])
        self.assertEqual(len(self.fixture.store.review_attempts), 1)

    def test_storage_failure_returns_retryable_receipt_with_frozen_candidate(self):
        with patch.object(self.fixture.service, "commit_practice_review", side_effect=RuntimeError("storage unavailable")):
            result = self.process()
        self.assertEqual(result["receipt_status"], "review_retryable")
        self.assertTrue(result["candidate_id"])
        code = self.job.checkpoint["review_manifest"][0]["code"]
        state = self.call("/v1/internal/harness/context", {"session_id": "review-test", "review_code": code})
        context = json.loads(state[2]["context_json"])
        self.assertEqual(context["review_item"]["recommended_action"], "retry_group_confirmation")
        retried = self.call("/v1/internal/harness/practice-reviews/retry", {
            "session_id": "review-test", "review_code": code,
        })
        self.assertEqual(retried[0], 200, retried)
        retried_result = json.loads(retried[2]["result_json"])
        self.assertEqual(retried_result["receipt"]["status"], "review_waiting")
        self.assertEqual(retried_result["review_item"]["recommended_action"], "submit_remaining_required")
        confirmed = self.call(f"/v1/internal/harness/grade-results/{result['candidate_id']}/commit", {"session_id": "review-test", "input_version": 1})
        self.assertEqual(confirmed[2]["receipt"]["status"], "review_waiting")

    def test_unmatched_result_can_be_linked_by_text_without_image(self):
        result = self.process(code=False)
        self.assertEqual(result["receipt_status"], "review_unmatched")
        self.assertEqual(result["review_association"]["status"], "unmatched")
        linked = self.call(f"/v1/internal/harness/grade-results/{result['candidate_id']}/commit", {
            "session_id": "review-test", "input_version": result["input_version"],
            "review": {"code": self.job.checkpoint["review_manifest"][0]["code"]}})
        self.assertEqual((linked[0], linked[2]["receipt"]["status"]), (200, "review_waiting"))
        self.assertEqual(len(self.fixture.store.files), 2)  # PDF + original photo

    def test_practice_adjudication_rejects_forgery_staleness_and_prevalidates_batch(self):
        recommendation = self.job.checkpoint["review_manifest"][1]

        def pending(owner, suffix):
            attempt_id = suffix * 32
            self.fixture.store.attempts[attempt_id] = Attempt(
                attempt_id, owner, (suffix.upper() * 32)[:32], 1,
                recommendation["stem_text"], "y=3或-3", "grade_ready",
            )
            return self.fixture.store.record_grade_candidate(
                user_id=owner, attempt_id=attempt_id, input_version=1, verdict="correct", first_error=None,
                evidence=json.dumps({"schema": "math-error-diagnosis/v1", "practice_review": {
                    "status": "unmatched", "locator": {"kind": "recommendation"},
                }}, ensure_ascii=False),
            )

        first, second = pending(self.fixture.owner, "1"), pending(self.fixture.owner, "2")
        foreign = pending("other", "3")
        self.fixture.store.bind_model_session(user_id="other", session_id="review-other")
        code = recommendation["code"]
        rationale = "题干条件、全部数值、所求量与唯一候选逐项一致，因此可以确定是同一道题。"

        denied = self.call("/v1/internal/harness/practice-reviews/adjudicate", {
            "session_id": "review-test", "items": [{"candidate_id": foreign.candidate_id,
                "input_version": 1, "status": "matched", "code": code, "rationale": rationale}],
        })
        self.assertEqual((denied[0], denied[2]["error"]["code"]), (404, "not_found"))
        stale = self.call("/v1/internal/harness/practice-reviews/adjudicate", {
            "session_id": "review-test", "items": [{"candidate_id": first.candidate_id,
                "input_version": 2, "status": "matched", "code": code, "rationale": rationale}],
        })
        self.assertEqual((stale[0], stale[2]["error"]["code"]), (409, "input_version_changed"))
        forged = self.call("/v1/internal/harness/practice-reviews/adjudicate", {
            "session_id": "review-test", "items": [{"candidate_id": first.candidate_id,
                "input_version": 1, "status": "matched", "code": "R" + "f" * 12 + "-99-ABCDEF",
                "rationale": rationale}],
        })
        self.assertEqual((forged[0], forged[2]["error"]["code"]), (400, "invalid_request"))

        before_candidates = deepcopy(self.fixture.store.candidates)
        before_checkpoint = deepcopy(self.fixture.store.jobs[self.job.job_id].checkpoint)
        batch = self.call("/v1/internal/harness/practice-reviews/adjudicate", {
            "session_id": "review-test", "items": [
                {"candidate_id": first.candidate_id, "input_version": 1, "status": "matched",
                 "code": code, "rationale": rationale},
                {"candidate_id": second.candidate_id, "input_version": 1, "status": "matched",
                 "code": "R" + "e" * 12 + "-98-ABCDEF", "rationale": rationale},
            ],
        })
        self.assertEqual((batch[0], batch[2]["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.fixture.store.candidates, before_candidates)
        self.assertEqual(self.fixture.store.jobs[self.job.job_id].checkpoint, before_checkpoint)

        for changed_code in (code.lower(), code.swapcase()):
            with self.subTest(changed_code=changed_code):
                rejected = self.call("/v1/internal/harness/practice-reviews/adjudicate", {
                    "session_id": "review-test", "items": [
                        {"candidate_id": first.candidate_id, "input_version": 1, "status": "matched",
                         "code": code, "rationale": rationale},
                        {"candidate_id": second.candidate_id, "input_version": 1, "status": "matched",
                         "code": changed_code, "rationale": rationale},
                    ],
                })
                self.assertEqual((rejected[0], rejected[2]["error"]["code"]), (400, "invalid_request"))
                self.assertEqual(self.fixture.store.candidates, before_candidates)
                self.assertEqual(self.fixture.store.jobs[self.job.job_id].checkpoint, before_checkpoint)

        uncertain = self.call("/v1/internal/harness/practice-reviews/adjudicate", {
            "session_id": "review-test", "items": [
                {"candidate_id": first.candidate_id, "input_version": 1, "status": "uncertain",
                 "code": "", "rationale": "现有图片证据不足，两个候选仍然都可能对应这道题，无法唯一确认。"},
                {"candidate_id": second.candidate_id, "input_version": 1, "status": "uncertain",
                 "code": "", "rationale": "现有图片证据不足，两个候选仍然都可能对应这道题，无法唯一确认。"},
            ],
        })
        self.assertEqual([item["status"] for item in uncertain[2]["results"]],
                         ["review_unmatched", "review_unmatched"])
        self.assertEqual(self.fixture.store.candidates, before_candidates)
        self.assertEqual(self.fixture.store.jobs[self.job.job_id].checkpoint, before_checkpoint)

        # A session may submit its own active page, not all account pending rows.
        accepted = self.call("/v1/internal/harness/practice-reviews/adjudicate", {
            "session_id": "review-test", "items": [
                {"candidate_id": first.candidate_id, "input_version": 1, "status": "matched",
                 "code": code, "rationale": rationale},
            ],
        })
        self.assertEqual(accepted[0], 200, accepted)
        self.assertEqual(accepted[2]["results"][0]["status"], "review_waiting")

    def test_inspection_preserves_bounded_pending_context(self):
        pending = [{"candidate_id": f"{index:032x}", "input_version": 1, "options": []}
                   for index in range(25)]
        with patch.object(self.fixture.service, "list_pending_practice_review_links", return_value=pending):
            response = self.call("/v1/internal/harness/context", {"session_id": "review-test"})
        self.assertEqual(response[0], 200, response)
        self.assertEqual(json.loads(response[2]["context_json"])["pending_review_links"], pending[:20])

    def test_legacy_review_code_links_latex_stem_to_ocr_text_without_image(self):
        checkpoint = deepcopy(self.job.checkpoint)
        item = checkpoint["review_manifest"][0]
        item["code"] = f"R{self.job.job_id[:12]}-01"
        item["stem_text"] = (
            r"【湖北重点高中2022高一联考】平面向量 $\vec a,\vec b$ 满足 "
            r"$|\vec a|=2,|\vec b|=1,|\vec a-2\vec b|=2|\vec a+\vec b|$。"
            r"求 $\vec a,\vec b$ 的夹角 $\theta$。"
        )
        self.job = replace(self.job, checkpoint=checkpoint)
        self.fixture.store.jobs[self.job.job_id] = self.job
        ocr_text = "【湖北重点高中2022高一联考】平面向量 a,b 满足 |a|=2,|b|=1,|a-2b|=2|a+b|。求 a,b 的夹角 θ。"
        result = self.process(code=False, question_text=ocr_text)
        self.assertEqual(result["receipt_status"], "review_unmatched")
        linked = self.call(f"/v1/internal/harness/grade-results/{result['candidate_id']}/commit", {
            "session_id": "review-test", "input_version": result["input_version"],
            "review": {"code": item["code"], "pdf_id": self.job.job_id, "error_id": item["error_id"],
                       "question_id": "", "stage": item["stage"], "kind": item["kind"]},
        })
        self.assertEqual((linked[0], linked[2]["receipt"]["status"]), (200, "review_waiting"))

    def test_legacy_review_code_preserves_latex_fraction_number_boundaries(self):
        checkpoint = deepcopy(self.job.checkpoint)
        item = checkpoint["review_manifest"][0]
        item["code"] = f"R{self.job.job_id[:12]}-01"
        item["stem_text"] = (
            r"【全国新高考Ⅰ2022·6】函数 $f(x)=\sin(\omega x+\frac{\pi}{4})+b$（$\omega>0$）"
            r"的最小正周期为 $T$，且 $\frac{2\pi}{3}<T<\pi$；图象关于点 "
            r"$(\frac{3\pi}{2},2)$ 中心对称。求 $f(\frac{\pi}{2})$。"
            r"选项：A.1；B.$\frac32$；C.$\frac52$；D.3。"
        )
        self.job = replace(self.job, checkpoint=checkpoint)
        self.fixture.store.jobs[self.job.job_id] = self.job
        ocr_text = (
            "【全国新高考 I 2022-6】函数 f(x)=sin(ωx+π/4)+B（ω>0）的最小正周期为 T，"
            "且 2π/3<T<π；图象关于点 (3π/2,2) 中心对称。求 f(π/2)。选项：A.1；B.3/2；C.5/2；D.3。"
        )
        result = self.process(code=False, question_text=ocr_text)
        linked = self.call(f"/v1/internal/harness/grade-results/{result['candidate_id']}/commit", {
            "session_id": "review-test", "input_version": result["input_version"],
            "review": {"code": item["code"], "pdf_id": self.job.job_id, "error_id": item["error_id"],
                       "question_id": "", "stage": item["stage"], "kind": item["kind"]},
        })
        self.assertEqual((result["receipt_status"], linked[0], linked[2]["receipt"]["status"]),
                         ("review_unmatched", 200, "review_waiting"))

    def test_legacy_review_code_normalizes_source_label_and_compact_fraction(self):
        checkpoint = deepcopy(self.job.checkpoint)
        item = checkpoint["review_manifest"][0]
        item["code"] = f"R{self.job.job_id[:12]}-01"
        item["stem_text"] = (
            r"【广东梅州2022摸底】$\triangle ABC$ 是边长为 $a$ 的等边三角形，$P$ 为平面 $ABC$ 内一点，"
            r"求 $\overrightarrow{PA}\cdot(\overrightarrow{PB}+\overrightarrow{PC})$ 的最小值。"
            r"选项：A.$-2a^2$；B.$-\frac38a^2$；C.$-\frac43a^2$；D.$-a^2$。"
        )
        self.job = replace(self.job, checkpoint=checkpoint)
        self.fixture.store.jobs[self.job.job_id] = self.job
        ocr_text = (
            "【广东梅州2022模拟】△ABC 是边长为 a 的等边三角形，P 为平面 ABC 内一点，"
            "求 PA·(PB+PC) 的最小值。选项：A.-2a²；B.-3/8a²；C.-4/3a²；D.-a²。"
        )
        context = self.fixture.service.resolve_practice_review(
            user_id=self.fixture.owner, question_text=ocr_text,
            locator={"code": item["code"], "error_id": item["error_id"][:8], "stage": 1, "kind": "original"},
            review_mode=True,
        )
        self.assertEqual((context["status"], context["code"]), ("matched", item["code"]))

    def test_legacy_review_code_does_not_treat_vector_b_as_a_choice(self):
        checkpoint = deepcopy(self.job.checkpoint)
        item = checkpoint["review_manifest"][0]
        item["code"] = f"R{self.job.job_id[:12]}-01"
        item["stem_text"] = (
            r"设 $|\vec a|=1$，$|\vec b|=2$，且 $\vec a,\vec b$ 的夹角为 $120^\circ$，"
            r"求 $(\vec a+\vec b)\cdot(\vec a-2\vec b)$ 与 $|2\vec a+\vec b|$。"
        )
        self.job = replace(self.job, checkpoint=checkpoint)
        self.fixture.store.jobs[self.job.job_id] = self.job
        context = self.fixture.service.resolve_practice_review(
            user_id=self.fixture.owner,
            question_text="设 |a|=1，|b|=2，且 a,b 的夹角为 120°，求 (a+b)·(a-2b) 与 |2a+b|。",
            locator={"code": item["code"], "error_id": item["error_id"][:8], "stage": 1, "kind": "original"},
            review_mode=True,
        )
        self.assertEqual((context["status"], context["code"]), ("matched", item["code"]))

    def test_one_photo_uses_its_confirmed_pdf_for_an_ambiguous_recommendation(self):
        current = self.job
        extra = Question("c" * 32, "已知二次方程 z 的平方等于十六，求 z。", "z=4或-4", 10, 2, "测试题库")
        self.fixture.store.add_question(extra)
        self.fixture.store.recommendations["r2"] = Recommendation(
            "r2", self.fixture.owner, self.fixture.error.error_id, extra, "同知识点", "assigned"
        )
        longer = self.fixture.paper(key="longer-reprint", plan_kind="practice")
        longer_checkpoint = deepcopy(longer.checkpoint)
        extra_item = dict(current.checkpoint["review_manifest"][1])
        extra_item.update(code=f"R{longer.job_id[:12]}-99-ABCDEF", question_id=extra.question_id,
                          stem_text=extra.stem_text)
        longer_checkpoint["review_manifest"].append(extra_item)
        self.fixture.store.jobs[longer.job_id] = replace(longer, checkpoint=longer_checkpoint)
        ambiguous = self.fixture.service.resolve_practice_review(
            user_id=self.fixture.owner, question_text=self.fixture.question.stem_text,
            locator={"question_id": self.fixture.question.question_id, "kind": "recommendation"}, review_mode=True,
        )
        self.assertEqual(ambiguous["status"], "unmatched")

        self.job = current
        with patch.object(self.client, "call", return_value=(200, {}, {"results": [{}]})):
            self.process(0)
        row = dict(self.last_payload["items"][0])
        recommendation = current.checkpoint["review_manifest"][1]
        row.update(item_no=2, question_text=recommendation["stem_text"],
                   review={"question_id": recommendation["question_id"], "kind": "recommendation"})
        self.last_payload["items"].append(row)
        response = self.call("/v1/internal/harness/intakes/process", self.last_payload)
        self.assertEqual(response[0], 200, response)
        associations = [item["review_association"] for item in response[2]["results"]]
        self.assertEqual([item["pdf_id"] for item in associations], [current.job_id, current.job_id])
        self.assertEqual(associations[1]["review_code"], recommendation["code"])

    def test_reference_conflict_blocks_review_until_adjudication(self):
        self.process()
        result = self.process(1, conflict=True, color="red")
        self.assertEqual(result["receipt_status"], "needs_review")
        self.assertIsNotNone(result["reference_review"])
        self.assertFalse(self.fixture.store.review_attempts)
        candidate = self.fixture.store.get_grade_candidate(user_id=self.fixture.owner, candidate_id=result["candidate_id"])
        with self.assertRaises(RuntimeError):
            self.fixture.store.commit_grade(user_id=self.fixture.owner, candidate_id=candidate.candidate_id, expected_version=1)
        diagnosis = json.loads(candidate.evidence)
        validation = diagnosis["cross_validation"]
        diagnosis["reference_adjudication"] = {"schema": "question-bank-reference-adjudication/v1", "status": "consistent", "rationale": "测试独立复核", "reference_answer_sha256": validation["reference_answer_sha256"], "independent_answer_sha256": validation["independent_answer_sha256"]}
        revised = replace(candidate, evidence=json.dumps(diagnosis))
        receipt = asyncio.run(self.client.app._commit_candidate_receipt(self.fixture.owner, revised))
        self.assertEqual(receipt["status"], "review_needs_correction")
        self.assertEqual(len(self.fixture.store.errors), 1)

    def numeric_conflict(self, *, code=False, semantic=False):
        question = replace(self.fixture.question,
            stem_text=r"已知函数 f(x) 的值域为 [-1,1]，函数 g(x)=[f(x)]^2+f(x)+\frac{5}{4}，由 g(x)=1 求 f(x) 的值。",
            answer_text="f(x)=-1/2")
        self.fixture.store.add_question(question)
        self.fixture.store.recommendations["r1"] = replace(self.fixture.store.recommendations["r1"], question=question)
        self.job = self.fixture.paper(key="numeric-paper", plan_kind="practice")
        with patch.object(self.client, "call", return_value=(200, {}, {"results": [{}]})):
            self.process(1, code=code, conflict=True, color="red")
        self.last_payload["items"][0]["question_text"] = question.stem_text.replace(r"\frac{5}{4}", r"\frac{3}{4}")
        if semantic:
            self.last_payload["items"][0]["review"] = {}
        response = self.call("/v1/internal/harness/intakes/process", self.last_payload)
        self.assertEqual(response[0], 200, response)
        result = response[2]["results"][0]
        self.assertEqual(result["receipt_status"], "needs_review")
        return result

    def adjudicate(self, result):
        response = self.call("/v1/internal/harness/reference-conflicts/adjudicate", {
            "session_id": "review-test", "items": [{
                "candidate_id": result["candidate_id"], "input_version": 1,
                "status": "conflict", "rationale": "图片常数识别与已验证题库不同，按题库当前版本重新核验学生作答为正确。",
                "authoritative_grade": {"verdict": "correct", "first_error": "", "cause_code": "", "cause_evidence": "",
                    "knowledge_points": ["方程"], "prevention_cue": "", "confidence": 0.99}}]})
        self.assertEqual(response[0], 200, response)
        return response[2]["results"][0]

    def test_corrected_candidate_survives_locator_confirmation_and_old_id_replay(self):
        original = self.numeric_conflict()
        revised = self.adjudicate(original)
        self.assertNotEqual(revised["candidate_id"], original["candidate_id"])
        self.assertEqual(revised["status"], "review_unmatched")
        self.assertFalse(self.fixture.store.jobs[self.job.job_id].checkpoint.get("review_submissions"))
        payload = {"session_id": "review-test", "input_version": 1,
                   "review": {"code": self.job.checkpoint["review_manifest"][1]["code"]}}
        path = f"/v1/internal/harness/grade-results/{original['candidate_id']}/commit"
        for _ in range(2):
            response = self.call(path, payload)
            self.assertEqual(response[0], 200, response)
            receipt = response[2]["receipt"]
            self.assertEqual((receipt["status"], receipt["reference_status"]), ("review_waiting", "consistent"))
            candidate = self.fixture.store.get_grade_candidate(user_id=self.fixture.owner, candidate_id=receipt["candidate_id"])
            self.assertEqual(candidate.verdict, "correct")
            self.assertIn("reference_match", json.loads(candidate.evidence)["practice_review"])
        submissions = self.fixture.store.jobs[self.job.job_id].checkpoint["review_submissions"]
        self.assertEqual([row["verdict"] for row in submissions.values()], ["correct"])
        self.assertFalse(self.fixture.store.review_attempts)  # Original still required.
        self.assertEqual(len(self.fixture.store.errors), 1)
        replay = self.call("/v1/internal/harness/intakes/process", self.last_payload)
        self.assertEqual(replay[2]["results"][0]["verdict"], "correct")
        self.assertIsNone(replay[2]["results"][0]["reference_review"])
        # A typo after linking must not move the saved result to another item.
        payload["review"]["code"] = self.job.checkpoint["review_manifest"][0]["code"]
        self.assertEqual(self.call(path, payload)[0], 400)

    def test_reference_adjudication_refreshes_pending_candidate_evidence(self):
        original = self.numeric_conflict(semantic=True)
        self.assertTrue(original["review_match_candidates"])
        self.assertIn("stem_text", original["review_match_candidates"][0])
        revised = self.adjudicate(original)
        self.assertNotEqual(revised["candidate_id"], original["candidate_id"])
        self.assertEqual(revised["status"], "review_unmatched")
        self.assertTrue(revised["review_match_candidates"])
        self.assertEqual(revised["question_text"], original["question_text"])

    def test_adjudication_automatically_links_numeric_ocr_difference(self):
        original = self.numeric_conflict(code=True)
        revised = self.adjudicate(original)
        self.assertEqual(revised["status"], "review_waiting")
        candidate = self.fixture.store.get_grade_candidate(user_id=self.fixture.owner, candidate_id=revised["candidate_id"])
        self.assertEqual((candidate.verdict, json.loads(candidate.evidence)["practice_review"]["status"]), ("correct", "matched"))

    def test_locator_cannot_bypass_unresolved_or_changed_reference(self):
        original = self.numeric_conflict()
        path = f"/v1/internal/harness/grade-results/{original['candidate_id']}/commit"
        payload = {"session_id": "review-test", "input_version": 1,
                   "review": {"code": self.job.checkpoint["review_manifest"][1]["code"]}}
        self.assertEqual(self.call(path, payload)[2]["receipt"]["status"], "needs_review")
        self.assertFalse(self.fixture.store.jobs[self.job.job_id].checkpoint.get("review_submissions"))
        self.adjudicate(original)
        question = self.fixture.store.questions[self.fixture.question.question_id]
        self.fixture.store.add_question(replace(question, version_id="d" * 32, version_no=2))
        self.assertEqual(self.call(path, payload)[0], 409)
        self.assertFalse(self.fixture.store.jobs[self.job.job_id].checkpoint.get("review_submissions"))

    def test_resolved_candidate_lookup_is_account_and_version_scoped(self):
        result = self.numeric_conflict()
        self.adjudicate(result)
        candidate = self.fixture.store.get_grade_candidate(user_id=self.fixture.owner, candidate_id=result["candidate_id"])
        self.assertIsNone(self.fixture.store.find_resolved_grade_candidate(user_id="other", candidate=candidate))
        self.assertIsNone(self.fixture.store.find_resolved_grade_candidate(user_id=self.fixture.owner, candidate=replace(candidate, input_version=2)))


if __name__ == "__main__":
    unittest.main()
