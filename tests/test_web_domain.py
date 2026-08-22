from __future__ import annotations

from pathlib import Path
import unittest

from services.web_domain import MySqlDomainStore


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

    def test_create_file_checks_active_user_and_scopes_dedupe(self) -> None:
        row = ("f" * 32, "a" * 32, "exam", "quarantine/x", "d" * 64, "application/pdf", 12, "ready")
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
        self.assertTrue(any("WHERE user_id=%s AND purpose=%s AND content_sha256=%s" in query for query, _ in connection.cursor_instance.executed))
        self.assertEqual(connection.committed, 1)

    def test_pending_job_count_is_scoped_to_server_user(self) -> None:
        connection = FakeConnection([(3,)])
        count = MySqlDomainStore(lambda: connection).pending_job_count(user_id="a" * 32)
        query, args = connection.cursor_instance.executed[-1]
        self.assertEqual(count, 3)
        self.assertIn("WHERE user_id=%s", query)
        self.assertEqual(args, ("a" * 32,))


if __name__ == "__main__":
    unittest.main()
