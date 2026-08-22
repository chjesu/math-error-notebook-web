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

    def test_load_input_is_bom_compatible_and_compact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text('\ufeff{ "scope": "public fixture" }', encoding="utf-8")
            self.assertEqual(router.load_input(path), '{"scope":"public fixture"}')

    def test_low_confidence_non_sol_result_escalates_once(self) -> None:
        value = {
            "route": router.select("web-implementation", []),
            "result": {"status": "complete", "confidence": 0.5},
        }
        self.assertTrue(router.needs_escalation(value))
        value["route"] = router.select("web-security-review", [])
        self.assertFalse(router.needs_escalation(value))

    def test_invoke_uses_read_only_ephemeral_codex_and_writes_metadata_only_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            output_path = temporary_path / "result.json"
            original_audits = router.AUDITS
            router.AUDITS = temporary_path / "audits"

            def fake_run(command, **kwargs):
                self.assertIn("--ephemeral", command)
                self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
                self.assertIn("--skip-git-repo-check", command)
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
                        router.select("web-requirements", []), '{"scope":"public fixture"}', output_path
                    )
            finally:
                router.AUDITS = original_audits
            audit = json.loads(Path(value["audit"]).read_text(encoding="utf-8"))
            self.assertNotIn("input", audit)
            self.assertFalse(audit["database_modified"])


if __name__ == "__main__":
    unittest.main()
