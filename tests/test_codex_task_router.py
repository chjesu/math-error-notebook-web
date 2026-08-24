from __future__ import annotations

import importlib.util
import json
from pathlib import Path
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
        intake_route = router.select("math-intake-candidate", [])
        self.assertEqual(intake_route["model"], "gpt-5.6-terra")
        self.assertTrue(intake_route["schema"].endswith("intake-candidate.schema.json"))
        adjudicated = router.select("math-grade-candidate", ["math_uncertainty"])
        self.assertEqual(adjudicated["model"], "gpt-5.6-sol")
        self.assertEqual(adjudicated["promoted_from"], "math-grade-candidate")

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
                self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
                self.assertIn("--skip-git-repo-check", command)
                self.assertEqual(command[command.index("-i") + 1], str(image_path))
                self.assertNotIn("public fixture", " ".join(command))
                self.assertIn("public fixture", kwargs["input"])
                self.assertNotEqual(Path(kwargs["cwd"]).resolve(), ROOT.resolve())
                output_path.write_text(
                    json.dumps(
                        {"status": "complete", "confidence": 0.99, "summary": "ok", "findings": []}
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            try:
                with mock.patch.object(router.shutil, "which", return_value="codex"), mock.patch.object(
                    router.subprocess, "run", side_effect=fake_run
                ):
                    value = router.invoke(
                        router.select("web-requirements", []), '{"scope":"public fixture"}', output_path, [image_path]
                    )
            finally:
                router.AUDITS = original_audits
            audit = json.loads(Path(value["audit"]).read_text(encoding="utf-8"))
            self.assertNotIn("input", audit)
            self.assertFalse(audit["database_modified"])

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
