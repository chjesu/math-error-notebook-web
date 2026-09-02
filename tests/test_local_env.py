from __future__ import annotations

from pathlib import Path
import inspect
import tempfile
from unittest import mock
import unittest

from scripts import local_env


class LocalEnvironmentTests(unittest.TestCase):
    def test_portable_bootstrap_reuses_the_authoritative_local_environment(self) -> None:
        script = (local_env.ROOT / "scripts" / "bootstrap_local.ps1").read_text(encoding="utf-8")
        self.assertIn("scripts\\local_env.py init", script)
        self.assertIn("scripts\\local_env.py smoke", script)
        self.assertIn('"--enable-codex-model"', script)
        self.assertNotIn("Remove-Item", script)

    def test_server_cannot_bind_outside_loopback(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "localhost"):
            local_env.serve("0.0.0.0", 8000)

    def test_local_server_fails_closed_without_pdf_rendering_dependencies(self) -> None:
        source = inspect.getsource(local_env.serve)
        for dependency in ("matplotlib", "PIL", "reportlab"):
            self.assertIn(f"import {dependency}", source)
        self.assertIn("scripts/bootstrap_local.ps1", source)

    def test_harness_web_command_uses_fixed_local_surface(self) -> None:
        command = local_env._harness_web_command()
        self.assertTrue(command[0].lower().endswith(("node", "node.exe")))
        self.assertEqual(Path(command[1]), local_env.ROOT / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js")
        self.assertEqual(command[2:4], ["--profile", "web"])
        self.assertEqual(command[4], "--patch")
        self.assertEqual(Path(command[5]), local_env.HARNESS_WEB_PATCH)
        self.assertEqual(command[6:], ["--host", "127.0.0.1", "--port", "3080", "--no-open"])
        self.assertEqual(local_env.HARNESS_PRODUCT_WORKSPACE.parent, local_env.HARNESS_WEB_HOME)
        self.assertTrue((local_env.HARNESS_AGENT_PRESETS / "math-notebook" / "agent.cordis.yml").is_file())
        self.assertEqual(
            local_env.HARNESS_RUNTIME_PRESET,
            local_env.HARNESS_WEB_HOME / ".agent-presets" / "math-notebook" / "agent.cordis.yml",
        )

    def test_harness_web_process_is_stopped_with_parent(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        local_env._stop_harness_web(process)
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)

    def test_harness_startup_replaces_only_the_stale_model_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(local_env, "HARNESS_WEB_HOME", Path(directory)), mock.patch.dict(
            "os.environ", {"HARNESS_MODEL": "qwen3.8-flash"}, clear=False
        ):
            settings = Path(directory) / "settings.yaml"
            settings.write_text(
                "ui-onboarding:\n  welcomeNoticeVersion: test\nagent-default-model:\n  provider: deepseek-official\n  model: deepseek-v4-flash\n  reasoningEffort: high\n",
                encoding="utf-8",
            )
            local_env._pin_harness_model_settings()
            self.assertEqual(
                settings.read_text(encoding="utf-8"),
                "ui-onboarding:\n  welcomeNoticeVersion: test\nagent-default-model:\n  provider: notebook-provider\n  model: qwen3.8-flash\n",
            )
            local_env._pin_harness_model_settings()
            self.assertNotIn("deepseek", settings.read_text(encoding="utf-8"))

    def test_harness_startup_rejects_an_invalid_model_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(local_env, "HARNESS_WEB_HOME", Path(directory)), mock.patch.dict(
            "os.environ", {"HARNESS_MODEL": "qwen\nmodel: injected"}, clear=False
        ):
            (Path(directory) / "settings.yaml").write_text("ui-onboarding: {}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "valid model id"):
                local_env._pin_harness_model_settings()

    def test_harness_receipt_bridge_token_is_runtime_only(self) -> None:
        source = inspect.getsource(local_env._start_harness_web)
        serve = inspect.getsource(local_env.serve)
        self.assertIn('"LZLM_HARNESS_INTERNAL_TOKEN": internal_token', source)
        self.assertIn('"LZLM_PRODUCT_ORIGIN": "http://127.0.0.1:8000"', source)
        self.assertIn("secrets.token_urlsafe(32)", serve)
        self.assertNotIn("test-internal-token", source + serve)

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
                "0004_learning_loop.sql",
                "0005_auth_v040.sql",
                "0006_privacy.sql",
                "0007_auth_security.sql",
                "0008_privacy_recovery.sql",
                "0009_file_upload_idempotency.sql",
                "0010_codex_harness.sql",
                "0011_daily_learning_usage.sql",
                "0012_operations_admin.sql",
                "0013_model_usage_sessions.sql",
                "0014_question_options.sql",
            ],
        )
        self.assertTrue(all(path.is_file() for path in local_env.MIGRATIONS))

    def test_domain_smoke_cleanup_never_deletes_the_shared_question_bank(self) -> None:
        general_cleanup = inspect.getsource(local_env._clear_test_data)
        scoped_cleanup = inspect.getsource(local_env._clear_domain_smoke_data)
        domain_smoke = inspect.getsource(local_env._domain_smoke)
        self.assertNotIn("question_sources", general_cleanup)
        self.assertIn("WHERE user_id=%s", scoped_cleanup)
        self.assertIn("DELETE FROM question_sources WHERE id=%s", scoped_cleanup)
        self.assertIn("if not recommendations or len(reviews) != 1", domain_smoke)

    def test_smoke_preserves_existing_local_users(self) -> None:
        source = inspect.getsource(local_env.smoke)
        self.assertIn("preserved_user_count = _existing_user_count()", source)
        self.assertNotIn("_clear_test_data()", source)
        self.assertIn("_clear_smoke_auth(service, [phone], [smoke_user.user_id])", source)

    def test_smoke_auth_cleanup_is_scoped_to_created_identities(self) -> None:
        source = inspect.getsource(local_env._clear_smoke_auth)
        self.assertIn("WHERE id IN", source)
        self.assertIn("WHERE user_id IN", source)
        self.assertIn("WHERE phone_lookup_hash IN", source)
        self.assertNotIn("DELETE FROM web_users\"", source)

    def test_live_schema_check_requires_matching_ledger_and_tables(self) -> None:
        ledger = "\n".join(
            f"{path.name}\t{__import__('hashlib').sha256(path.read_bytes()).hexdigest()}"
            for path in local_env.MIGRATIONS
        )
        with mock.patch.object(local_env, "_is_running", return_value=True), mock.patch.object(
            local_env, "_run_sql", side_effect=[ledger, "8", "3"]
        ):
            self.assertTrue(local_env._live_schema_ready())
        with mock.patch.object(local_env, "_is_running", return_value=True), mock.patch.object(
            local_env, "_run_sql", side_effect=[ledger, "7", "3"]
        ):
            self.assertFalse(local_env._live_schema_ready())

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

    def test_partially_applied_0005_resumes_with_idempotent_0007(self) -> None:
        migration = local_env.ROOT / "services" / "web_auth" / "migrations" / "0005_auth_v040.sql"
        partial = False
        labels: list[str] = []

        def run_sql(sql, *, label, **kwargs):
            nonlocal partial
            del kwargs
            labels.append(label)
            if label == "migration ledger shape check":
                return "1"
            if label == "migration ledger read":
                return ""
            if label == "0005 partial schema check":
                return "1" if partial else "0"
            if label == "apply 0005_auth_v040.sql" and not partial:
                partial = True
                raise RuntimeError("simulated middle DDL failure")
            if label == "apply 0005_auth_v040.sql":
                self.assertIn("CREATE TABLE IF NOT EXISTS", sql)
            return ""

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            local_env, "MIGRATIONS", (migration,)
        ), mock.patch.object(local_env, "_run_sql", side_effect=run_sql), mock.patch.object(
            local_env, "READY", Path(directory) / "ready"
        ):
            with self.assertRaisesRegex(RuntimeError, "middle DDL failure"):
                local_env._apply_migrations()
            local_env._apply_migrations()
        self.assertEqual(labels.count("record 0005_auth_v040.sql"), 1)

    def test_unledgered_atomic_0008_is_detected_and_recorded(self) -> None:
        migration = local_env.ROOT / "services" / "web_domain" / "migrations" / "0008_privacy_recovery.sql"
        labels: list[str] = []

        def run_sql(sql, *, label, **kwargs):
            del kwargs
            labels.append(label)
            if label == "migration ledger shape check":
                return "1"
            if label == "migration ledger read":
                return ""
            if label == "0008 recovery schema check":
                return "3"
            if label == "apply 0008_privacy_recovery.sql":
                self.assertEqual(sql, "SELECT 1;")
            return ""

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            local_env, "MIGRATIONS", (migration,)
        ), mock.patch.object(local_env, "_run_sql", side_effect=run_sql), mock.patch.object(
            local_env, "READY", Path(directory) / "ready"
        ):
            local_env._apply_migrations()
        self.assertIn("record 0008_privacy_recovery.sql", labels)

    def test_unledgered_0014_is_detected_and_recorded(self) -> None:
        migration = local_env.ROOT / "services" / "web_domain" / "migrations" / "0014_question_options.sql"
        labels: list[str] = []

        def run_sql(sql, *, label, **kwargs):
            del kwargs
            labels.append(label)
            if label == "migration ledger shape check":
                return "1"
            if label == "migration ledger read":
                return ""
            if label == "0014 question options recovery schema check":
                return "1"
            if label == "apply 0014_question_options.sql":
                self.assertEqual(sql, "SELECT 1;")
            return ""

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            local_env, "MIGRATIONS", (migration,)
        ), mock.patch.object(local_env, "_run_sql", side_effect=run_sql), mock.patch.object(
            local_env, "READY", Path(directory) / "ready"
        ):
            local_env._apply_migrations()
        self.assertIn("record 0014_question_options.sql", labels)


if __name__ == "__main__":
    unittest.main()
