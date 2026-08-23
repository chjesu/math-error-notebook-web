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
        with self.assertRaisesRegex(ValueError, "dependencies"):
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

    def test_project_template_exposes_parallel_ready_work_and_routes(self) -> None:
        started = workflow.start("WEB-PROJECT-001", "full web project", "project")
        self.assertEqual(started["ready_steps"], ["product_baseline"])
        workflow.claim("WEB-PROJECT-001", "product_baseline", "PO", "product", 60)
        workflow.update(
            "WEB-PROJECT-001", "product_baseline", "product", "completed", "prd.md", None, None
        )
        workflow.claim("WEB-PROJECT-001", "architecture_and_contract", "ARCH", "architect", 60)
        result = workflow.update(
            "WEB-PROJECT-001",
            "architecture_and_contract",
            "architect",
            "completed",
            "architecture.md",
            None,
            None,
        )
        self.assertEqual(
            result["ready_steps"], ["identity_and_sms", "domain_data", "file_pipeline"]
        )
        manifest = workflow.read("WEB-PROJECT-001")
        identity = workflow.step(manifest, "identity_and_sms")[1]
        self.assertEqual(identity["model_task"], "web-implementation")

    def test_project_security_reviewer_is_separate_from_implementation(self) -> None:
        workflow.start("WEB-PROJECT-002", "full web project", "project")
        payload = workflow.read("WEB-PROJECT-002")
        for item in payload["steps"]:
            if item["name"] == "system_security_review":
                item["dependencies"] = []
            if item["name"] == "identity_and_sms":
                item["owner"] = "same-agent"
        workflow.write("WEB-PROJECT-002", payload)
        with self.assertRaisesRegex(ValueError, "cannot be the only reviewer"):
            workflow.claim(
                "WEB-PROJECT-002", "system_security_review", "SEC", "same-agent", 60
            )

    def test_team_template_parallelizes_independent_design_reviews(self) -> None:
        started = workflow.start("WEB-TEAM-001", "Codex multi-agent team", "team")
        self.assertEqual(started["ready_steps"], ["team_charter"])
        workflow.claim("WEB-TEAM-001", "team_charter", "DM", "delivery-manager", 60)
        result = workflow.update(
            "WEB-TEAM-001",
            "team_charter",
            "delivery-manager",
            "completed",
            "docs/team-charter.md",
            None,
            None,
        )
        self.assertEqual(
            result["ready_steps"],
            ["role_design", "orchestration_design", "security_governance"],
        )
        manifest = workflow.read("WEB-TEAM-001")
        self.assertEqual(
            workflow.step(manifest, "orchestration_design")[1]["model_task"],
            "web-implementation",
        )
        self.assertEqual(
            workflow.step(manifest, "security_governance")[1]["model_task"],
            "web-security-review",
        )
        validation = workflow.step(manifest, "team_validation")[1]
        self.assertEqual(
            validation["separation_from"],
            ["role_design", "orchestration_design", "security_governance"],
        )
        for name, role in (
            ("role_design", "PO"),
            ("orchestration_design", "AI"),
            ("security_governance", "SEC"),
        ):
            workflow.claim("WEB-TEAM-001", name, role, "same-agent", 60)
            workflow.update(
                "WEB-TEAM-001", name, "same-agent", "completed", f"{name}.json", None, None
            )
        with self.assertRaisesRegex(ValueError, "cannot be the only reviewer"):
            workflow.claim(
                "WEB-TEAM-001", "team_validation", "QA", "same-agent", 60
            )

    def test_legacy_registration_manifest_keeps_sequential_dependencies(self) -> None:
        payload = {
            "schema": "web-registration-workflow/v1",
            "id": "WEB-LEGACY-001",
            "label": "legacy",
            "steps": [
                {
                    "name": name,
                    "role": role,
                    "human_only": human_only,
                    "status": "pending",
                    "owner": None,
                    "lease_expires_at": None,
                    "artifacts": [],
                    "note": "",
                }
                for name, role, human_only, *_ in workflow.REGISTRATION_STEPS
            ],
        }
        workflow.write("WEB-LEGACY-001", payload)
        loaded = workflow.read("WEB-LEGACY-001")
        implementation = workflow.step(loaded, "implementation")[1]
        self.assertEqual(implementation["dependencies"], ["architecture"])
        with self.assertRaisesRegex(ValueError, "dependencies"):
            workflow.claim("WEB-LEGACY-001", "implementation", "BE", "builder", 60)


if __name__ == "__main__":
    unittest.main()
