from __future__ import annotations

import io
from pathlib import Path
import inspect
import tempfile
from unittest import mock
import unittest

from scripts import local_env


class LocalEnvironmentTests(unittest.TestCase):
    def test_web_runtime_declares_and_checks_websocket_transport(self) -> None:
        requirements = (local_env.ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertRegex(requirements, r"(?m)^websockets>=13")
        self.assertRegex(requirements, r"(?m)^psutil>=5\.9")
        self.assertIn("import websockets", inspect.getsource(local_env.doctor))
        self.assertIn("import psutil", inspect.getsource(local_env.doctor))

    def test_portable_bootstrap_reuses_the_authoritative_local_environment(self) -> None:
        script = (local_env.ROOT / "scripts" / "bootstrap_local.ps1").read_text(encoding="utf-8")
        self.assertIn("scripts\\local_env.py init", script)
        self.assertIn("scripts\\local_env.py smoke", script)
        self.assertIn('"--enable-codex-model"', script)
        self.assertNotIn("Remove-Item", script)

    def test_server_cannot_bind_outside_loopback(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "localhost"):
            local_env.serve("0.0.0.0", 8000)

    def test_harness_web_command_uses_fixed_local_surface(self) -> None:
        with mock.patch.object(local_env.shutil, "which", return_value="node"):
            command = local_env._harness_web_command(0)
        self.assertTrue(command[0].lower().endswith(("node", "node.exe")))
        self.assertEqual(Path(command[1]), local_env.ROOT / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js")
        self.assertEqual(command[2:4], ["--profile", "web"])
        self.assertEqual(command[4], "--patch")
        self.assertEqual(Path(command[5]), local_env.HARNESS_WEB_PATCH)
        self.assertEqual(command[6:], ["--host", "127.0.0.1", "--port", "0", "--no-open"])
        self.assertEqual(local_env.HARNESS_PRODUCT_WORKSPACE.parent, local_env.HARNESS_WEB_HOME)
        self.assertTrue((local_env.HARNESS_AGENT_PRESETS / "math-notebook" / "agent.cordis.yml").is_file())
        self.assertEqual(
            local_env.HARNESS_RUNTIME_PRESET,
            local_env.HARNESS_WEB_HOME / ".agent-presets" / "math-notebook" / "agent.cordis.yml",
        )

    def test_harness_uses_child_announced_os_port_without_exposing_it_in_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "harness.log"
            ports = local_env.Queue(maxsize=1)
            local_env._capture_harness_web_output(
                io.StringIO("booting\ndsh web: http://127.0.0.1:43123\n"),
                log_path,
                ports,
            )
            log = log_path.read_text(encoding="utf-8")

        self.assertEqual(ports.get_nowait(), 43123)
        self.assertIn("http://127.0.0.1:<internal>", log)
        self.assertNotIn("43123", log)
        self.assertIsNone(local_env._harness_web_port_from_line("dsh web: http://attacker.test:43123"))

    def test_harness_stderr_is_private_and_cannot_publish_the_internal_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "harness.stderr.log"
            local_env._capture_harness_web_output(
                io.StringIO(
                    "failed http://localhost:43123; retry ws://127.0.0.1:43123; "
                    "fallback wss://[::1]:43123\n"
                ),
                log_path,
            )
            log = log_path.read_text(encoding="utf-8")

        self.assertNotIn("43123", log)
        self.assertEqual(log.count("<internal>"), 3)

    def test_harness_web_process_is_stopped_with_parent(self) -> None:
        process = mock.Mock()
        process.pid = 43123
        process._lzlm_job_handle = 99
        process.poll.return_value = None
        with mock.patch.object(local_env.sys, "platform", "win32"), mock.patch.object(
            local_env, "_terminate_windows_job", return_value=True
        ) as terminate_job, mock.patch.object(local_env, "_close_windows_job"):
            local_env._stop_harness_web(process)

        terminate_job.assert_called_once_with(process)
        process.wait.assert_called_once_with(timeout=5)

    def test_harness_stop_falls_back_to_exact_windows_process_tree(self) -> None:
        process = mock.Mock()
        process.pid = 43123
        process._lzlm_job_handle = None
        process.poll.return_value = None
        with mock.patch.object(local_env.sys, "platform", "win32"), mock.patch.object(
            local_env.subprocess, "run"
        ) as run, mock.patch.object(local_env, "_close_windows_job"):
            local_env._stop_harness_web(process)

        self.assertEqual(
            run.call_args.args[0],
            ["taskkill", "/F", "/T", "/PID", "43123"],
        )

    def test_harness_process_group_is_owned_on_every_platform(self) -> None:
        source = inspect.getsource(local_env._start_harness_web)
        self.assertIn("CREATE_NEW_PROCESS_GROUP", source)
        self.assertIn("WINDOWS_CREATE_SUSPENDED", source)
        self.assertEqual(local_env.WINDOWS_CREATE_SUSPENDED, 0x00000004)
        resume_source = inspect.getsource(local_env._resume_windows_process)
        self.assertIn("ResumeThread.restype = wintypes.DWORD", resume_source)
        self.assertIn("previous_suspend_count == 0xFFFFFFFF", resume_source)
        self.assertIn("previous_suspend_count == 0", resume_source)
        self.assertIn('start_new_session=sys.platform != "win32"', source)
        self.assertLess(
            source.index("_assign_windows_kill_job(process)"),
            source.index("_resume_windows_process(process)"),
        )

        process = mock.Mock()
        process.pid = 43123
        process.poll.return_value = None
        with mock.patch.object(local_env.sys, "platform", "linux"), mock.patch.object(
            local_env.os, "killpg", create=True
        ) as killpg:
            local_env._stop_harness_web(process)

        killpg.assert_called_once_with(43123, local_env.signal.SIGTERM)

    def test_windows_daemon_stop_terminates_the_recorded_process_tree(self) -> None:
        result = mock.Mock(returncode=0)
        with mock.patch.object(local_env.sys, "platform", "win32"), mock.patch.object(
            local_env, "_pid_is_running", side_effect=[True, False]
        ), mock.patch.object(local_env.subprocess, "run", return_value=result) as run:
            local_env._stop_service_tree(43123)

        command = run.call_args.args[0]
        self.assertEqual(command, ["taskkill", "/F", "/T", "/PID", "43123"])

    def test_windows_daemon_stop_rejects_nonzero_tree_kill(self) -> None:
        result = mock.Mock(returncode=1)
        with mock.patch.object(local_env.sys, "platform", "win32"), mock.patch.object(
            local_env, "_pid_is_running", return_value=True
        ), mock.patch.object(local_env.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "shutdown failed"):
                local_env._stop_service_tree(43123)

    def test_unix_daemon_stop_signals_the_recorded_process_group(self) -> None:
        with mock.patch.object(local_env.sys, "platform", "linux"), mock.patch.object(
            local_env.os, "killpg", create=True
        ) as killpg, mock.patch.object(
            local_env, "_process_group_is_running", side_effect=[True, False]
        ):
            local_env._stop_service_tree(43123)

        killpg.assert_called_once_with(43123, local_env.signal.SIGTERM)

    def test_failed_service_tree_stop_retains_pid_for_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "service.pid"
            identity = {
                "version": 1,
                "state": "running",
                "pid": 43123,
                "created_at_us": 123456789,
                "executable": "c:\\python\\python.exe",
            }
            pid_file.write_text(local_env.json.dumps(identity), encoding="utf-8")
            with mock.patch.object(local_env, "SERVICE_PID_FILE", pid_file), mock.patch.object(
                local_env, "_stop_service_tree", side_effect=RuntimeError("simulated stop failure")
            ), mock.patch.object(local_env, "_process_identity", return_value=identity), mock.patch.object(
                local_env, "_is_running", return_value=False
            ):
                with self.assertRaisesRegex(RuntimeError, "service process tree"):
                    local_env.stop()

            self.assertTrue(pid_file.is_file())

    def test_service_pid_record_binds_pid_to_creation_time_and_executable(self) -> None:
        identity = {
            "version": 1,
            "state": "running",
            "pid": 43123,
            "created_at_us": 123456789,
            "executable": "c:\\python\\python.exe",
        }
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "service.pid"
            with mock.patch.object(local_env, "SERVICE_PID_FILE", pid_file), mock.patch.object(
                local_env, "_process_identity", return_value=identity
            ):
                claim_token = local_env._claim_service_pid()
                local_env._write_service_pid(43123, claim_token)
                self.assertEqual(local_env._read_service_pid(), 43123)
            self.assertEqual(local_env.json.loads(pid_file.read_text(encoding="utf-8")), identity)

    def test_daemon_start_claim_is_exclusive_before_a_child_is_created(self) -> None:
        daemon_source = inspect.getsource(local_env.main)
        self.assertLess(
            daemon_source.index("_claim_service_pid()"),
            daemon_source.index("subprocess.Popen("),
        )
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "service.pid"
            with mock.patch.object(local_env, "SERVICE_PID_FILE", pid_file):
                claim_token = local_env._claim_service_pid()
                with self.assertRaisesRegex(RuntimeError, "already claimed"):
                    local_env._claim_service_pid()
                claim = local_env.json.loads(pid_file.read_text(encoding="utf-8"))
                self.assertEqual(claim["state"], "starting")
                self.assertEqual(claim["claim_token"], claim_token)
                local_env._release_service_pid_claim(claim_token)
            self.assertFalse(pid_file.exists())

    def test_failed_pid_write_and_failed_tree_cleanup_preserve_recovery_identity(self) -> None:
        identity = {
            "version": 1,
            "state": "running",
            "pid": 43123,
            "created_at_us": 123456789,
            "executable": "c:\\python\\python.exe",
        }
        process = mock.Mock()
        process.pid = 43123
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "service.pid"
            with mock.patch.object(local_env, "SERVICE_PID_FILE", pid_file), mock.patch.object(
                local_env, "_process_identity", return_value=identity
            ), mock.patch.object(
                local_env, "_stop_service_tree", side_effect=RuntimeError("simulated cleanup failure")
            ):
                claim_token = local_env._claim_service_pid()
                with self.assertRaisesRegex(RuntimeError, "recovery identity retained"):
                    local_env._handle_failed_daemon_start(process, claim_token)
                recovery = local_env.json.loads(pid_file.read_text(encoding="utf-8"))
                self.assertEqual(recovery["state"], "recovery")
                self.assertEqual(recovery["pid"], 43123)
                self.assertEqual(local_env._read_service_pid(), 43123)

    def test_stale_service_pid_identity_is_retained_without_killing_reused_pid(self) -> None:
        recorded = {
            "version": 1,
            "state": "running",
            "pid": 43123,
            "created_at_us": 123456789,
            "executable": "c:\\python\\python.exe",
        }
        reused = {**recorded, "created_at_us": 987654321}
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "service.pid"
            pid_file.write_text(local_env.json.dumps(recorded), encoding="utf-8")
            with mock.patch.object(local_env, "SERVICE_PID_FILE", pid_file), mock.patch.object(
                local_env, "_process_identity", return_value=reused
            ), mock.patch.object(local_env, "_stop_service_tree") as stop_tree, mock.patch.object(
                local_env, "_is_running", return_value=False
            ):
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    local_env.stop()

            stop_tree.assert_not_called()
            self.assertTrue(pid_file.is_file())

    def test_service_pid_unlink_failure_is_reported_and_record_is_retained(self) -> None:
        identity = {
            "version": 1,
            "state": "running",
            "pid": 43123,
            "created_at_us": 123456789,
            "executable": "c:\\python\\python.exe",
        }
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "service.pid"
            pid_file.write_text(local_env.json.dumps(identity), encoding="utf-8")
            with mock.patch.object(local_env, "SERVICE_PID_FILE", pid_file), mock.patch.object(
                local_env, "_process_identity", return_value=identity
            ), mock.patch.object(local_env, "_stop_service_tree"), mock.patch.object(
                local_env.Path, "unlink", side_effect=OSError("simulated unlink failure")
            ), mock.patch.object(local_env, "_is_running", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "PID record"):
                    local_env.stop()

            self.assertTrue(pid_file.is_file())

    def test_harness_receipt_bridge_token_is_runtime_only(self) -> None:
        source = inspect.getsource(local_env._start_harness_web)
        serve = inspect.getsource(local_env.serve)
        self.assertIn('"LZLM_HARNESS_INTERNAL_TOKEN": internal_token', source)
        self.assertIn('"LZLM_PRODUCT_ORIGIN": product_origin', source)
        self.assertIn("secrets.token_urlsafe(32)", serve)
        self.assertIn("harness_upstream", serve)
        self.assertNotIn("test-internal-token", source + serve)

    def test_service_scripts_expose_only_the_gateway_port(self) -> None:
        for name in ("start.ps1", "start.sh", "stop.ps1", "stop.sh"):
            script = (local_env.ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertNotIn("3080", script, name)
        self.assertNotIn("HARNESS_WEB_PORT", inspect.getsource(local_env))
        windows_stop = (local_env.ROOT / "scripts" / "stop.ps1").read_text(encoding="utf-8")
        unix_stop = (local_env.ROOT / "scripts" / "stop.sh").read_text(encoding="utf-8")
        self.assertNotIn("Stop-Process", windows_stop)
        self.assertNotIn("Remove-Item", windows_stop)
        self.assertNotIn("kill -9", unix_stop)
        self.assertNotIn("pkill", unix_stop)

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
                "0012_async_intake_batches.sql",
                "0013_learning_profile_views.sql",
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
            local_env, "_run_sql", side_effect=[ledger, "6", "3"]
        ):
            self.assertTrue(local_env._live_schema_ready())
        with mock.patch.object(local_env, "_is_running", return_value=True), mock.patch.object(
            local_env, "_run_sql", side_effect=[ledger, "5", "3"]
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


if __name__ == "__main__":
    unittest.main()
