from __future__ import annotations

from pathlib import Path
import tempfile
from unittest import mock
import unittest

from scripts import local_env


class LocalEnvironmentTests(unittest.TestCase):
    def test_server_cannot_bind_outside_loopback(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "localhost"):
            local_env.serve("0.0.0.0", 8000)

    def test_mysql_password_is_not_put_on_process_command_line(self) -> None:
        args = local_env._client_args(root=True)
        self.assertTrue(any(item.startswith("--defaults-extra-file=") for item in args))
        self.assertFalse(any(item.startswith("--password=") for item in args))
        self.assertIn("--batch", args)
        self.assertIn("--skip-column-names", args)

    def test_local_environment_applies_auth_and_domain_migrations(self) -> None:
        self.assertEqual(
            [path.name for path in local_env.MIGRATIONS],
            [
                "0001_phone_registration.sql",
                "0002_web_domain.sql",
                "0003_account_simplification.sql",
            ],
        )
        self.assertTrue(all(path.is_file() for path in local_env.MIGRATIONS))

    def test_failed_migration_is_not_recorded_and_can_resume(self) -> None:
        labels: list[str] = []

        def run_sql(sql, *, label, **kwargs):
            del sql, kwargs
            labels.append(label)
            if label == "migration ledger shape check":
                return "1"
            if label == "migration ledger read":
                return ""
            if label == "legacy schema check":
                return "0"
            if label == "apply 0002_web_domain.sql":
                raise RuntimeError("simulated DDL failure")
            return ""

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(local_env, "_run_sql", side_effect=run_sql), mock.patch.object(local_env, "READY", Path(directory) / "ready"):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                local_env._apply_migrations()
        self.assertIn("record 0001_phone_registration.sql", labels)
        self.assertNotIn("record 0002_web_domain.sql", labels)

    def test_applied_migration_hash_mismatch_fails_closed(self) -> None:
        def run_sql(sql, *, label, **kwargs):
            del sql, kwargs
            if label == "migration ledger shape check":
                return "1"
            if label == "migration ledger read":
                return "0001_phone_registration.sql\t" + "0" * 64
            return ""

        with mock.patch.object(local_env, "_run_sql", side_effect=run_sql):
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                local_env._apply_migrations()


if __name__ == "__main__":
    unittest.main()
