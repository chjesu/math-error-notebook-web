"""Local MySQL review check: all synthetic rows are rolled back, never committed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import uuid

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.local_env import _connection_factory
from services.web_domain import MySqlDomainStore, NotebookService


class SavepointConnection:
    """Exercise the store's transactions under one disposable outer transaction."""

    def __init__(self, connection):
        self.connection = connection

    def cursor(self):
        return self.connection.cursor()

    def begin(self):
        with self.cursor() as cursor:
            cursor.execute("SAVEPOINT pdf_review_check")

    def commit(self):
        with self.cursor() as cursor:
            cursor.execute("RELEASE SAVEPOINT pdf_review_check")

    def rollback(self):
        with self.cursor() as cursor:
            cursor.execute("ROLLBACK TO SAVEPOINT pdf_review_check")

    def close(self):
        pass


def main():
    connection = _connection_factory()()
    owner = uuid.uuid4().hex
    current = datetime.now(timezone.utc)
    try:
        connection.begin()
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO web_users (id,phone_lookup_hash,phone_last4,status,created_at,updated_at) VALUES (%s,%s,'0000','active',%s,%s)",
                           (owner, uuid.uuid4().hex + uuid.uuid4().hex, current.replace(tzinfo=None), current.replace(tzinfo=None)))
        store = MySqlDomainStore(lambda: SavepointConnection(connection))
        with TemporaryDirectory(prefix="pdf-review-check-") as temporary:
            service = NotebookService(store, Path(temporary))

            def grade(color, evidence, verdict):
                output = BytesIO()
                Image.new("RGB", (24, 24), color).save(output, format="PNG")
                file = service.upload(user_id=owner, purpose="question_image", original_name="test.png", content=output.getvalue())
                intake, _ = store.create_intake(user_id=owner, file_id=file.file_id, idempotency_key=uuid.uuid4().hex)
                intake = store.save_extraction_candidate(user_id=owner, intake_id=intake.intake_id,
                    question_text="已知一元一次方程 x+5=10，求未知数 x 的值。", answer_text="x=5", evidence={})
                attempt_id, _ = store.confirm_intake(user_id=owner, intake_id=intake.intake_id, expected_version=1, idempotency_key=uuid.uuid4().hex)
                return store.record_grade_candidate(user_id=owner, attempt_id=attempt_id, input_version=1,
                    verdict=verdict, first_error="移项错误" if verdict == "incorrect" else None, evidence=json.dumps(evidence, ensure_ascii=False))

            first = grade("white", {"schema": "math-error-diagnosis/v1", "knowledge_points": ["方程"]}, "incorrect")
            error = store.commit_grade(user_id=owner, candidate_id=first.candidate_id, expected_version=1)
            with connection.cursor() as cursor:
                cursor.execute("UPDATE review_tasks SET due_at=%s WHERE user_id=%s", ((current - timedelta(days=2)).replace(tzinfo=None), owner))
            job = service.create_practice_pdf(user_id=owner, error_ids=[error.error_id], idempotency_key="transaction-paper")
            item = job.checkpoint["review_manifest"][0]
            repeated = service.create_practice_pdf(user_id=owner, error_ids=[error.error_id], idempotency_key="transaction-repeat")
            assert repeated.job_id == job.job_id
            reprint = service.create_practice_pdf(user_id=owner, error_ids=[error.error_id], idempotency_key="transaction-reprint", plan_kind="practice")
            context = service.resolve_practice_review(user_id=owner, question_text=item["stem_text"], locator={"code": item["code"]})
            assert context["status"] == "matched"
            candidate = grade("blue", {"schema": "math-error-diagnosis/v1", "practice_review": context, "knowledge_points": ["方程"]}, "correct")
            receipt = service.commit_practice_review(user_id=owner, candidate=candidate, now=current)
            assert receipt["status"] == "review_completed", receipt
            assert receipt["next_stage"] == 2
            assert datetime.fromisoformat(receipt["next_due_at"]) == current + timedelta(days=1)
            replay = service.commit_practice_review(user_id=owner, candidate=candidate, now=current + timedelta(days=2))
            assert replay["replayed"] and replay["completed_at"] == receipt["completed_at"]
            reprint_item = reprint.checkpoint["review_manifest"][0]
            reprint_context = service.resolve_practice_review(user_id=owner, question_text=reprint_item["stem_text"], locator={"code": reprint_item["code"]})
            reprint_candidate = grade("green", {"schema": "math-error-diagnosis/v1", "practice_review": reprint_context, "knowledge_points": ["方程"]}, "correct")
            replay = service.commit_practice_review(user_id=owner, candidate=reprint_candidate, now=current + timedelta(days=2))
            assert replay["replayed"] and replay["completed_at"] == receipt["completed_at"]
            papers = service.list_practice_pdfs(user_id=owner)
            assert len(papers) == 2 and all(paper["progress"]["answered_count"] == 1 for paper in papers)
            calendar = service.review_calendar(user_id=owner, month=current.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m"), now=current)
            assert calendar["summary"]["submitted_question_count"] == 1
            progress = store.progress(user_id=owner)
            assert progress["error_count"] == 1 and progress["completed_review_count"] == 1
            assert progress["today_completed_review_count"] == 1
            assert store.list_practice_pdfs(user_id="0" * 32) == []
            # An operation failing after the shared state machine must roll back
            # both the task mutation and its paper checkpoint.
            second = service.create_practice_pdf(user_id=owner, error_ids=[error.error_id], idempotency_key="rollback-paper", plan_kind="practice")
            task = store.list_active_reviews(user_id=owner)[0]
            def fail(checkpoint, get_task, complete):
                assert get_task(task.task_id)
                complete(task.task_id, "wrong", "rollback-check", current + timedelta(days=1))
                checkpoint["should_not_persist"] = True
                raise RuntimeError("simulated checkpoint failure")
            try:
                store.mutate_practice_checkpoint(user_id=owner, job_id=second.job_id, operation=fail, share_reviews=True)
            except RuntimeError:
                pass
            else:
                raise AssertionError("failure not raised")
            assert "should_not_persist" not in store.get_job(user_id=owner, job_id=second.job_id).checkpoint
            assert store.list_active_reviews(user_id=owner)[0] == task
            assert store.progress(user_id=owner)["completed_review_count"] == 1
            second_item = second.checkpoint["review_manifest"][0]
            second_context = service.resolve_practice_review(user_id=owner, question_text=second_item["stem_text"],
                                                            locator={"code": second_item["code"]})
            wrong = grade("yellow", {"practice_review": second_context}, "incorrect")
            submitted = current + timedelta(days=1)
            wrong_receipt = service.commit_practice_review(user_id=owner, candidate=wrong, now=submitted)
            assert wrong_receipt["status"] == "review_needs_correction"
            before_tasks = store.list_active_reviews(user_id=owner)
            corrected = grade("red", {"practice_review": second_context | {"correction": True}}, "correct")
            corrected_receipt = service.commit_practice_review(user_id=owner, candidate=corrected, now=submitted + timedelta(hours=1))
            assert corrected_receipt["status"] == "review_corrected"
            assert service.commit_practice_review(user_id=owner, candidate=corrected, now=submitted + timedelta(hours=2))["replayed"]
            assert store.list_active_reviews(user_id=owner) == before_tasks
            assert store.progress(user_id=owner)["completed_review_count"] == 2
            assert store.get_job(user_id=owner, job_id=second.job_id).checkpoint["review_receipts"][second_item["task_id"]] == wrong_receipt
            assert service.progress(user_id=owner, now=submitted)["today_needs_correction_count"] == 0
            print(json.dumps({"mysql_transaction": "passed", "cross_day": "passed", "reprints": "passed", "activity_dedup": "passed", "replay": "passed", "rollback": "passed", "settled_correction": "passed", "synthetic_rows": "rolled_back"}))
    finally:
        connection.rollback()
        connection.close()


if __name__ == "__main__":
    main()
