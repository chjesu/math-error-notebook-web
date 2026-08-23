from __future__ import annotations

import hashlib
from pathlib import Path
import unittest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "web_auth"
    / "migrations"
    / "0001_phone_registration.sql"
)
MIGRATION_0005 = MIGRATION.with_name("0005_auth_v040.sql")
MIGRATION_0007 = MIGRATION.with_name("0007_auth_security.sql")


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

    def test_applied_0005_bytes_remain_immutable(self) -> None:
        digest = hashlib.sha256(MIGRATION_0005.read_bytes()).hexdigest()
        self.assertEqual(
            digest,
            "1ae137d2abb26204327bf5bc03db6eaf75775eb06d0f99510e96d4bb50ed358d",
        )

    def test_0007_is_file_level_recoverable_after_partial_failure(self) -> None:
        sql = MIGRATION_0007.read_text(encoding="utf-8").lower()
        self.assertNotIn("drop table", sql)
        self.assertNotIn("create table auth_", sql)
        self.assertIn("create table if not exists auth_password_credentials", sql)
        self.assertIn("create table if not exists auth_agreement_acceptances", sql)
        self.assertIn("modify column purpose", sql)


if __name__ == "__main__":
    unittest.main()
