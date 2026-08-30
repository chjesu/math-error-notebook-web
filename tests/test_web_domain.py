from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from services.web_domain import InMemoryNotebookStore, MySqlDomainStore, NotebookService


class FakeCursor:
    def __init__(self, fetches=()) -> None:
        self.fetches = list(fetches)
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, query: str, args: tuple = ()) -> int:
        self.executed.append((" ".join(query.split()), args))
        return 1

    def fetchone(self):
        return self.fetches.pop(0) if self.fetches else None

    def fetchall(self):
        return self.fetches.pop(0) if self.fetches else []

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self, fetches=()) -> None:
        self.cursor_instance = FakeCursor(fetches)
        self.committed = self.rolled_back = 0

    def begin(self) -> None:
        pass

    def cursor(self):
        return self.cursor_instance

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        pass


class DomainContractTests(unittest.TestCase):
    def test_calendar_reads_cross_month_history_with_account_scope_and_no_writes(self) -> None:
        owner = "a" * 32
        due, completed = datetime(2026, 7, 31), datetime(2026, 8, 3)
        connection = FakeConnection([(1,), [], [("e" * 32, "题目", "错因", '{"knowledge_points":["向量"]}', 1, due, "completed", "t" * 32, due)],
                                     [("e" * 32, "题目", "错因", None, 1, "correct", completed, "t" * 32)]])
        result = MySqlDomainStore(lambda: connection).review_calendar(user_id=owner, month="2026-08", now=datetime(2026, 8, 4, tzinfo=timezone.utc))
        days = {day["date"]: day for day in result["days"]}
        self.assertEqual(len(days["2026-08-01"]["backlog_indices"]), 1)
        self.assertEqual(days["2026-08-03"]["backlog_indices"], [])
        for query, args in connection.cursor_instance.executed:
            self.assertTrue(query.startswith("SELECT "))
            self.assertEqual(args[0], owner)
            self.assertIn("user_id=%s", query)
        self.assertIn("a.review_task_id", connection.cursor_instance.executed[-1][0])
        self.assertEqual(connection.cursor_instance.executed[-1][1], (owner,))
        self.assertEqual(connection.committed, 0)

    def test_schema_has_only_direct_user_ownership(self) -> None:
        sql = (Path(__file__).resolve().parents[1] / "services" / "web_domain" / "migrations" / "0002_web_domain.sql").read_text(encoding="utf-8").lower()
        for removed in ("web_tenants", "tenant_memberships", "web_students", "tenant_invitations", "student_id"):
            self.assertNotIn(removed, sql)
        for table in ("web_files", "intake_items", "web_jobs", "attempts", "grade_candidates", "error_notebook_entries", "recommendations", "review_tasks"):
            body = sql.split(f"create table if not exists {table} (", 1)[1].split(") engine=", 1)[0]
            self.assertIn("user_id char(32)", body)

        learning_sql = (Path(__file__).resolve().parents[1] / "services" / "web_domain" / "migrations" / "0004_learning_loop.sql").read_text(encoding="utf-8").lower()
        self.assertIn("create table if not exists review_attempts", learning_sql)
        self.assertIn("unique key uq_review_attempts_request (user_id, idempotency_key)", learning_sql)

    def test_personal_reads_match_id_and_server_user(self) -> None:
        connection = FakeConnection()
        store = MySqlDomainStore(lambda: connection)
        self.assertIsNone(store.get_file(user_id="a" * 32, file_id="f" * 32))
        query, args = connection.cursor_instance.executed[-1]
        self.assertIn("user_id=%s AND id=%s", query)
        self.assertEqual(args, ("a" * 32, "f" * 32))

    def test_get_file_returns_original_name_from_the_same_projection(self) -> None:
        row = ("f" * 32, "a" * 32, "practice_pdf", "quarantine/x", "d" * 64, "application/pdf", 12, "ready", "practice.pdf")
        connection = FakeConnection([row])
        record = MySqlDomainStore(lambda: connection).get_file(user_id="a" * 32, file_id="f" * 32)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.original_name, "practice.pdf")
        self.assertIn("status,original_name FROM web_files", connection.cursor_instance.executed[-1][0])

    def test_create_file_checks_active_user_and_scopes_dedupe(self) -> None:
        row = ("f" * 32, "a" * 32, "exam", "quarantine/x", "d" * 64, "application/pdf", 12, "ready", "paper.pdf")
        connection = FakeConnection([("a" * 32,), row])
        record = MySqlDomainStore(lambda: connection).create_file(
            user_id="a" * 32,
            purpose="exam",
            original_name="paper.pdf",
            object_key="quarantine/x",
            content_sha256="d" * 64,
            media_type="application/pdf",
            byte_size=12,
        )
        self.assertEqual(record.user_id, "a" * 32)
        self.assertEqual(record.original_name, "paper.pdf")
        self.assertTrue(any("WHERE user_id=%s AND purpose=%s AND content_sha256=%s" in query for query, _ in connection.cursor_instance.executed))
        self.assertEqual(connection.committed, 1)

    def test_pending_job_count_is_scoped_to_server_user(self) -> None:
        connection = FakeConnection([(3,)])
        count = MySqlDomainStore(lambda: connection).pending_job_count(user_id="a" * 32)
        query, args = connection.cursor_instance.executed[-1]
        self.assertEqual(count, 3)
        self.assertIn("WHERE user_id=%s", query)
        self.assertEqual(args, ("a" * 32,))

    def test_learning_usage_counts_distinct_recommended_questions(self) -> None:
        connection = FakeConnection([[("recommendation", "counted", 24)], (16,)])

        usage = MySqlDomainStore(lambda: connection).learning_usage(user_id="a" * 32)

        self.assertEqual(usage["recommendation"]["count"], 16)
        self.assertIn("COUNT(DISTINCT r.question_id)", connection.cursor_instance.executed[-1][0])

    def test_practice_pdf_history_is_scoped_to_server_user(self) -> None:
        connection = FakeConnection([[]])
        items = MySqlDomainStore(lambda: connection).list_practice_pdfs(user_id="a" * 32)
        query, args = connection.cursor_instance.executed[-1]
        self.assertEqual(items, [])
        self.assertIn("WHERE j.user_id=%s", query)
        self.assertEqual(args, ("a" * 32,))

    def test_practice_pdf_history_uses_imported_filename(self) -> None:
        checkpoint = '{"file_id":"file","filename":"历史练习.pdf","question_count":0,"source":"desktop_skill"}'
        connection = FakeConnection([[("j" * 32, checkpoint, datetime(2026, 8, 29), "stored.pdf", 12)]])
        item = MySqlDomainStore(lambda: connection).list_practice_pdfs(user_id="a" * 32)[0]
        self.assertEqual((item["filename"], item["source"]), ("历史练习.pdf", "desktop_skill"))

    def test_mysql_commit_rejects_correct_candidate(self) -> None:
        row = ("a" * 32, 1, "correct", None, "pending", "题目", "答案", None)
        connection = FakeConnection([row])
        store = MySqlDomainStore(lambda: connection)
        with self.assertRaisesRegex(RuntimeError, "failed_final"):
            store.commit_grade(user_id="u" * 32, candidate_id="c" * 32, expected_version=1)
        self.assertEqual((connection.committed, connection.rolled_back), (0, 1))

    def test_duplicate_upload_discards_unreferenced_quarantine_file(self) -> None:
        with TemporaryDirectory() as temporary:
            service = NotebookService(InMemoryNotebookStore(), Path(temporary))
            content = b"\x89PNG\r\n\x1a\nimage"
            first = service.upload(user_id="a" * 32, purpose="question_image", original_name="q.png", content=content)
            second = service.upload(user_id="a" * 32, purpose="question_image", original_name="q.png", content=content)
            self.assertEqual(first.file_id, second.file_id)
            self.assertEqual(len([path for path in Path(temporary).rglob("*") if path.is_file()]), 1)

    def test_failed_file_metadata_write_discards_quarantine_file(self) -> None:
        class FailingStore:
            def create_file(self, **_kwargs):
                raise RuntimeError("database unavailable")

        with TemporaryDirectory() as temporary:
            service = NotebookService(FailingStore(), Path(temporary))
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                service.upload(user_id="a" * 32, purpose="question_image", original_name="q.png", content=b"\x89PNG\r\n\x1a\nimage")
            self.assertFalse(any(path.is_file() for path in Path(temporary).rglob("*")))


if __name__ == "__main__":
    unittest.main()
