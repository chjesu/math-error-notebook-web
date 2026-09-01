from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_task_router", ROOT / "scripts/codex_task_router.py")
router = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(router)


class CodexTaskRouterTests(unittest.TestCase):
    def test_documented_direct_script_route_command(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                "-B",
                "scripts/codex_task_router.py",
                "route",
                "--task",
                "web-security-review",
                "--json",
            ],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["task"], "web-security-review")

    def test_routes_by_difficulty_and_promotes_security_risk(self) -> None:
        self.assertEqual(router.select("web-requirements", [])["model"], "gpt-5.6-luna")
        self.assertEqual(router.select("web-implementation", [])["model"], "gpt-5.6-terra")
        self.assertEqual(router.select("web-security-review", [])["model"], "gpt-5.6-sol")
        promoted = router.select("web-implementation", ["authentication"])
        self.assertEqual(promoted["model"], "gpt-5.6-sol")
        self.assertEqual(promoted["promoted_from"], "web-implementation")
        math_route = router.select("math-grade-candidate", [])
        self.assertEqual(math_route["model"], "gpt-5.6-terra")
        self.assertTrue(math_route["schema"].endswith("math-grade-result.schema.json"))
        self.assertEqual(router.select("math-grade-solution", [])["reasoning_effort"], "medium")
        self.assertEqual(router.select("math-grade-solution-hard", [])["reasoning_effort"], "xhigh")
        self.assertEqual(router.select("math-grade-solution-max", [])["reasoning_effort"], "max")
        self.assertTrue(router.select("math-grade-solution", [])["schema"].endswith("math-solution-result.schema.json"))
        intake_route = router.select("math-intake-candidate", [])
        self.assertEqual(intake_route["model"], "gpt-5.6-terra")
        self.assertTrue(intake_route["schema"].endswith("intake-candidate.schema.json"))
        adjudicated = router.select("math-grade-candidate", ["math_uncertainty"])
        self.assertEqual(adjudicated["model"], "gpt-5.6-sol")
        self.assertEqual(adjudicated["promoted_from"], "math-grade-candidate")
        loop_route = router.select("math-notebook-loop", [])
        self.assertEqual(loop_route["model"], "gpt-5.6-sol")
        self.assertTrue(loop_route["schema"].endswith("math-loop-turn.schema.json"))

    def test_team_roles_route_by_specialty_and_wave(self) -> None:
        self.assertEqual(router.select_role("PO", [])["model"], "gpt-5.6-luna")
        self.assertEqual(router.select_role("BE", [])["model"], "gpt-5.6-terra")
        self.assertEqual(router.select_role("SEC", [])["model"], "gpt-5.6-sol")
        self.assertEqual(router.select_role("AUTH", [])["model"], "gpt-5.6-sol")
        self.assertEqual(router.select_role("DATA", [])["model"], "gpt-5.6-sol")
        self.assertEqual(router.select_role("MATH", [])["task"], "web-expert-review")
        self.assertEqual(router.select_role("ARCH", [])["reasoning_effort"], "high")
        promoted = router.select_role("BE", ["authentication"])
        self.assertEqual(promoted["model"], "gpt-5.6-sol")
        acceptance = router.select_wave("acceptance", [])
        self.assertEqual([route["role"] for route in acceptance], ["QA", "SRE", "MATH", "SEC"])

    def test_team_configuration_has_unique_known_members(self) -> None:
        value = router.team_config()
        self.assertLessEqual(value["max_parallel_agents"], 8)
        for members in value["waves"].values():
            self.assertEqual(len(members), len(set(members)))
            self.assertTrue(set(members) <= set(value["roles"]))

    def test_load_input_is_bom_compatible_and_compact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text('\ufeff{ "scope": "public fixture" }', encoding="utf-8")
            self.assertEqual(router.load_input(path), '{"scope":"public fixture"}')

    def test_math_grade_input_is_frozen_and_rejects_identity_fields(self) -> None:
        valid = {"attempt_id": "a" * 32, "input_version": 1, "question_text": "题目", "answer_text": "作答", "evidence": None}
        router.validate_grade_input(valid)
        with self.assertRaisesRegex(ValueError, "only"):
            router.validate_grade_input(valid | {"user_id": "u" * 32})

    def test_math_solution_schema_avoids_unsupported_unique_items_keyword(self) -> None:
        schema_path = Path(router.select("math-grade-solution", [])["schema"])
        self.assertNotIn('"uniqueItems"', schema_path.read_text(encoding="utf-8"))

    def test_math_intake_input_is_frozen_and_image_only(self) -> None:
        valid = {"intake_id": "b" * 32, "input_version": 1, "media_type": "image/png"}
        router.validate_intake_input(valid)
        with self.assertRaisesRegex(ValueError, "only"):
            router.validate_intake_input(valid | {"user_id": "u" * 32})
        with self.assertRaisesRegex(ValueError, "PNG or JPEG"):
            router.validate_intake_input(valid | {"media_type": "application/pdf"})

    def test_low_confidence_non_sol_result_escalates_once(self) -> None:
        value = {
            "route": router.select("web-implementation", []),
            "result": {"status": "complete", "confidence": 0.5},
        }
        self.assertTrue(router.needs_escalation(value))
        value["route"] = router.select("web-security-review", [])
        self.assertFalse(router.needs_escalation(value))
        math_value = {
            "route": router.select("math-grade-candidate", []),
            "result": {"verdict": "incorrect", "confidence": 0.95},
        }
        self.assertFalse(router.needs_escalation(math_value))
        math_value["result"] = {"verdict": "unclear", "confidence": 0.95}
        self.assertTrue(router.needs_escalation(math_value))
        solution_value = {"route": router.select("math-grade-solution", []), "result": {"confidence": 0.95}}
        self.assertFalse(router.needs_escalation(solution_value))

    def test_invoke_uses_read_only_ephemeral_codex_and_writes_metadata_only_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            output_path = temporary_path / "result.json"
            image_path = (temporary_path / "question.png").resolve()
            image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            original_audits = router.AUDITS
            router.AUDITS = temporary_path / "audits"

            def fake_run(command, **kwargs):
                self.assertIn("--ephemeral", command)
                self.assertIn("--ignore-user-config", command)
                self.assertEqual(command[command.index("--disable") + 1], "shell_tool")
                if "resume" in command:
                    self.assertIn('sandbox_mode="read-only"', command)
                else:
                    self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
                self.assertIn("--skip-git-repo-check", command)
                self.assertEqual(command[command.index("-i") + 1], str(image_path))
                self.assertNotIn("public fixture", " ".join(command))
                self.assertIn("public fixture", kwargs["input"])
                self.assertNotEqual(Path(kwargs["cwd"]).resolve(), ROOT.resolve())
                self.assertEqual(kwargs["env"]["USERPROFILE"], str(temporary_path))
                self.assertEqual(kwargs["env"]["CODEX_HOME"], str(temporary_path / ".codex"))
                self.assertEqual(kwargs["env"]["HTTPS_PROXY"], "http://127.0.0.1:8080")
                output_path.write_text(
                    json.dumps(
                        {"status": "complete", "confidence": 0.99, "summary": "ok", "findings": []}
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            try:
                with mock.patch.dict(router.os.environ, {"USERPROFILE": str(temporary_path), "HTTPS_PROXY": "http://127.0.0.1:8080"}), mock.patch.object(
                    router.shutil, "which", return_value="codex"
                ), mock.patch.object(router.subprocess, "run", side_effect=fake_run):
                    value = router.invoke(
                        router.select("web-requirements", []), '{"scope":"public fixture"}', output_path, [image_path]
                    )
            finally:
                router.AUDITS = original_audits
            audit = json.loads(Path(value["audit"]).read_text(encoding="utf-8"))
            self.assertNotIn("input", audit)
            self.assertFalse(audit["database_modified"])
            self.assertEqual(audit["cli_attempts"], 1)

    def test_transient_cli_failure_retries_and_logs_only_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "result.json"
            original_audits = router.AUDITS
            router.AUDITS = root / "audits"
            calls = 0

            def fake_run(command, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return SimpleNamespace(returncode=1, stdout="", stderr="error sending request for url")
                output.write_text(json.dumps({"status": "complete", "confidence": 0.99, "summary": "ok", "findings": []}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            try:
                with mock.patch.object(router.shutil, "which", return_value="codex"), mock.patch.object(
                    router.subprocess, "run", side_effect=fake_run
                ), mock.patch.object(router.time, "sleep") as sleep:
                    value = router.invoke(router.select("web-requirements", []), '{"secret":"must-not-be-logged"}', output)
            finally:
                router.AUDITS = original_audits
            self.assertEqual(calls, 2)
            sleep.assert_called_once_with(router.CLI_RETRY_DELAY_SECONDS)
            self.assertEqual(value["result"]["status"], "complete")
            events = [json.loads(line) for line in (root / "audits" / "codex-cli-events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["outcome"] for event in events], ["retrying", "success"])
            self.assertNotIn("must-not-be-logged", json.dumps(events))

    def test_certificate_failure_is_not_retried_and_has_public_network_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_audits = router.AUDITS
            router.AUDITS = root / "audits"
            try:
                with mock.patch.object(router.shutil, "which", return_value="codex"), mock.patch.object(
                    router.subprocess, "run",
                    return_value=SimpleNamespace(returncode=1, stdout="", stderr="invalid peer certificate: UnknownIssuer"),
                ) as run:
                    with self.assertRaises(router.CodexCliInvocationError) as raised:
                        router.invoke(router.select("web-requirements", []), '{"scope":"public"}', root / "result.json")
            finally:
                router.AUDITS = original_audits
            self.assertEqual(run.call_count, 1)
            self.assertEqual((raised.exception.category, raised.exception.public_code), ("certificate", "model_network_error"))

    def test_conversation_turn_starts_and_resumes_one_read_only_codex_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "turn.json"
            original_audits = router.AUDITS
            router.AUDITS = root / "audits"
            thread_ids = []

            def fake_turn(*, route, prompt, output_path, thread_id, event_callback):
                thread_ids.append(thread_id)
                packet = json.loads(prompt.split("Review input:\n", 1)[1])
                result = {
                    "conversation_id": packet["conversation_id"], "stage": packet["stage"],
                    "resource_id": packet["resource_id"], "input_version": packet["input_version"],
                    "action": "respond", "assistant_message": "继续", "question_text": None,
                    "answer_text": None, "verdict": None, "first_error": None, "cause_code": None,
                    "cause_evidence": None, "knowledge_points": [], "correct_solution": None, "final_answer": None,
                    "prevention_cue": None, "confidence": 0.99,
                }
                output_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                return {"thread_id": thread_id or "thread-123", "result": result}

            packet = json.dumps({"conversation_id": "a" * 32, "stage": "intake", "resource_id": "a" * 32, "input_version": 1, "user_message": "继续", "context": {}}, ensure_ascii=False)
            try:
                with mock.patch.object(router, "run_app_server_turn", side_effect=fake_turn):
                    first = router.run_conversation_turn(router.select("math-notebook-loop", []), packet, output)
                    second = router.run_conversation_turn(router.select("math-notebook-loop", []), packet, output, first["session_id"])
            finally:
                router.AUDITS = original_audits
            self.assertEqual((first["session_id"], second["session_id"]), ("thread-123", "thread-123"))
            self.assertEqual(thread_ids, [None, "thread-123"])

    def test_app_server_retries_only_before_a_turn_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "turn.json"
            original_audits = router.AUDITS
            router.AUDITS = root / "audits"
            packet = json.dumps({"conversation_id": "a" * 32, "stage": "intake", "resource_id": "a" * 32, "input_version": 1, "user_message": "继续", "context": {}})
            result = {"conversation_id": "a" * 32, "stage": "intake", "resource_id": "a" * 32, "input_version": 1, "action": "ready", "assistant_message": "可以确认", "confidence": 0.9}
            try:
                with mock.patch.object(router.time, "sleep"), mock.patch.object(
                    router, "run_app_server_turn",
                    side_effect=[router.AppServerError("network", category="network"), {"thread_id": "thread-1", "result": result}],
                ) as run:
                    value = router.run_conversation_turn(router.select("math-notebook-loop", []), packet, output)
                self.assertEqual((run.call_count, value["audit"] is not None), (2, True))
                with mock.patch.object(
                    router, "run_app_server_turn",
                    side_effect=router.AppServerError("network", category="network", turn_started=True),
                ) as run:
                    with self.assertRaises(router.CodexCliInvocationError):
                        router.run_conversation_turn(router.select("math-notebook-loop", []), packet, output)
                self.assertEqual(run.call_count, 1)
            finally:
                router.AUDITS = original_audits

    def test_structured_harness_turn_sends_local_images_and_resumes_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = (root / "question.png").resolve()
            image.write_bytes(b"synthetic")
            output = root / "result.json"
            original_audits = router.AUDITS
            router.AUDITS = root / "audits"
            frozen = {"intake_id": "a" * 32, "input_version": 1, "media_type": "image/png"}
            result = {**frozen, "status": "complete", "items": [{"item_no": 1}], "notes": None, "confidence": 0.99}

            def fake_turn(*, route, prompt, output_path, images, thread_id, event_callback):
                self.assertEqual((images, thread_id), ([image], "thread-existing"))
                self.assertIn("every distinct question", prompt)
                return {"thread_id": thread_id, "result": result}

            try:
                with mock.patch.object(router, "run_app_server_turn", side_effect=fake_turn):
                    value = router.run_structured_harness_turn(
                        router.select("math-intake-adjudication", []),
                        json.dumps(frozen), output, [image], "thread-existing",
                    )
            finally:
                router.AUDITS = original_audits
            self.assertEqual((value["thread_id"], value["result"]["status"]), ("thread-existing", "complete"))

    def test_run_wave_dispatches_one_independent_result_per_role(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            original_results = router.TEAM_RESULTS
            router.TEAM_RESULTS = Path(temporary)

            def fake_review(route, review_input, output_path):
                self.assertIn('"classification":"public-synthetic"', review_input)
                return {"route": route, "result": {"status": "complete"}, "out": str(output_path)}

            packets = {
                role: '{"classification":"public-synthetic","question":"q","sources":[]}'
                for role in ("DM", "PO", "UX")
            }
            try:
                with mock.patch.object(router, "invoke", side_effect=fake_review):
                    value = router.run_wave(
                        "product", [], packets, Path(temporary) / "product-run"
                    )
                    with self.assertRaisesRegex(ValueError, "must not already exist"):
                        router.run_wave("product", [], packets, Path(temporary) / "product-run")
            finally:
                router.TEAM_RESULTS = original_results
            self.assertEqual(value["parallel_agents"], 3)
            self.assertEqual(set(value["results"]), {"DM", "PO", "UX"})

    def test_team_input_requires_public_role_specific_packets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            original_inputs = router.TEAM_INPUTS
            router.TEAM_INPUTS = Path(temporary)
            path = Path(temporary) / "product.json"
            packets = {
                role: {"question": "公开问题", "sources": []}
                for role in ("DM", "PO", "UX")
            }
            try:
                path.write_text(
                    json.dumps(
                        {"classification": "public-synthetic", "wave": "product", "packets": packets},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                loaded = router.load_team_input(path, "product")
                self.assertEqual(set(loaded), {"DM", "PO", "UX"})
                value = json.loads(path.read_text(encoding="utf-8"))
                value["classification"] = "project-confidential"
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "public-synthetic"):
                    router.load_team_input(path, "product")
                with self.assertRaisesRegex(ValueError, "must stay under"):
                    router.resolve_under(Path(temporary).parent / "outside.json", Path(temporary))
            finally:
                router.TEAM_INPUTS = original_inputs


if __name__ == "__main__":
    unittest.main()
