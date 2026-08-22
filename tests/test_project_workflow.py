from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("project_workflow", ROOT / "scripts/project_workflow.py")
workflow = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(workflow)


class ProjectWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.original = workflow.WORKFLOW_DIR
        workflow.WORKFLOW_DIR = Path(self.temp.name)

    def tearDown(self) -> None:
        workflow.WORKFLOW_DIR = self.original
        self.temp.cleanup()

    def test_order_role_review_separation_and_human_gate(self) -> None:
        started = workflow.start("WEB-AUTH-001", "registration")
        self.assertEqual(started["next_step"], "requirements")
        with self.assertRaisesRegex(ValueError, "previous"):
            workflow.claim("WEB-AUTH-001", "implementation", "BE", "builder", 60)
        with self.assertRaisesRegex(ValueError, "requires role"):
            workflow.claim("WEB-AUTH-001", "requirements", "BE", "builder", 60)

        owners = {
            "requirements": ("PO", "product"),
            "architecture": ("ARCH", "architect"),
            "implementation": ("BE", "builder"),
            "deterministic_tests": ("QA", "qa"),
            "luna_requirements_review": ("PO", "luna-reviewer"),
            "terra_implementation_review": ("ARCH", "terra-reviewer"),
            "sol_security_review": ("SEC", "security-reviewer"),
            "integration_test": ("QA", "integration-qa"),
            "deploy_approval": ("DM", "delivery-manager"),
        }
        for name, (role, owner) in owners.items():
            workflow.claim("WEB-AUTH-001", name, role, owner, 60)
            if name == "deploy_approval":
                with self.assertRaisesRegex(ValueError, "human-approved"):
                    workflow.update(
                        "WEB-AUTH-001", name, owner, "completed", "approval.md", None, None
                    )
                result = workflow.update(
                    "WEB-AUTH-001",
                    name,
                    owner,
                    "completed",
                    "approval.md",
                    None,
                    "human-dm",
                )
            else:
                result = workflow.update(
                    "WEB-AUTH-001", name, owner, "completed", f"{name}.json", None, None
                )
        self.assertTrue(result["complete"])

    def test_implementation_owner_cannot_self_review(self) -> None:
        workflow.start("WEB-AUTH-002", "registration")
        sequence = (
            ("requirements", "PO", "product"),
            ("architecture", "ARCH", "architect"),
            ("implementation", "BE", "same-agent"),
            ("deterministic_tests", "QA", "qa"),
            ("luna_requirements_review", "PO", "luna"),
        )
        for name, role, owner in sequence:
            workflow.claim("WEB-AUTH-002", name, role, owner, 60)
            workflow.update("WEB-AUTH-002", name, owner, "completed", "evidence", None, None)
        with self.assertRaisesRegex(ValueError, "cannot be the only reviewer"):
            workflow.claim(
                "WEB-AUTH-002", "terra_implementation_review", "ARCH", "same-agent", 60
            )


if __name__ == "__main__":
    unittest.main()

