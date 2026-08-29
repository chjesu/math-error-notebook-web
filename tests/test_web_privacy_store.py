from __future__ import annotations

import unittest

from services.web_domain import MySqlDomainStore


class Cursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, query: str, args: tuple = ()) -> int:
        self.executed.append((" ".join(query.split()), args))
        return 1

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self) -> None:
        pass


class Connection:
    def __init__(self) -> None:
        self.cursor_instance = Cursor()

    def begin(self) -> None:
        pass

    def cursor(self) -> Cursor:
        return self.cursor_instance

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class PrivacyStoreTests(unittest.TestCase):
    def test_every_export_table_is_scoped_to_the_server_user(self) -> None:
        connection = Connection()
        user_id = "a" * 32
        data = MySqlDomainStore(lambda: connection).export_data(user_id=user_id)
        self.assertEqual(set(data), {"schema_version", "files", "intakes", "attempts", "grade_candidates", "errors", "recommendations", "learning_usage", "review_tasks", "review_attempts", "jobs"})
        self.assertEqual(len(connection.cursor_instance.executed), 10)
        for query, args in connection.cursor_instance.executed:
            self.assertIn("WHERE user_id=%s", query)
            self.assertEqual(args, (user_id,))

    def test_export_download_claim_is_scoped_counted_and_audited(self) -> None:
        class DownloadCursor(Cursor):
            def __init__(self) -> None:
                super().__init__()
                self.rows = [("j" * 32,), (2,)]

            def fetchone(self):
                return self.rows.pop(0)

        connection = Connection()
        connection.cursor_instance = DownloadCursor()
        allowed = MySqlDomainStore(lambda: connection).claim_export_download(user_id="a" * 32, job_id="j" * 32, maximum=3)
        self.assertTrue(allowed)
        statements = connection.cursor_instance.executed
        self.assertIn("id=%s AND user_id=%s AND job_type='export'", statements[0][0])
        self.assertEqual(statements[0][1], ("j" * 32, "a" * 32))
        self.assertIn("event_type='export.downloaded'", statements[1][0])
        self.assertEqual(statements[1][1], ("a" * 32, "j" * 32))
        self.assertIn("INSERT INTO domain_audit_events", statements[2][0])


if __name__ == "__main__":
    unittest.main()
