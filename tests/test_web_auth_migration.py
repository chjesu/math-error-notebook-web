from __future__ import annotations

from pathlib import Path
import unittest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "web_auth"
    / "migrations"
    / "0001_phone_registration.sql"
)


def table_body(sql: str, table: str) -> str:
    return sql.split(f"CREATE TABLE {table} (", 1)[1].split(") ENGINE=", 1)[0]


class WebAuthMigrationTests(unittest.TestCase):
    def test_challenge_has_every_column_required_by_store(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        body = table_body(sql, "auth_sms_challenges")
        for column in (
            "phone_lookup_hash",
            "tenant_scope_hash",
            "code_hash",
            "provider_receipt",
            "consumed_at",
        ):
            self.assertIn(column, body)

    def test_global_user_identity_does_not_require_tenant_scope(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertNotIn("tenant_scope_hash", table_body(sql, "web_users"))


if __name__ == "__main__":
    unittest.main()
