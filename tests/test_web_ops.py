from pathlib import Path
import unittest

from services.web_ops import InMemoryOperationsStore, OperationsService


class OperationsServiceTests(unittest.TestCase):
    def test_roles_receive_only_their_declared_sections(self) -> None:
        expected = {
            "operations": {"overview", "users", "behavior", "usage", "tasks", "risk"},
            "reviewer": {"overview", "tasks", "content"},
            "security": {"overview", "risk", "privacy", "audit"},
            "administrator": {"overview", "users", "behavior", "usage", "tasks", "content", "risk", "privacy", "audit"},
        }
        for role, sections in expected.items():
            with self.subTest(role=role):
                store = InMemoryOperationsStore()
                store.grant(user_id="user-" + role, role=role)
                result = OperationsService(store).dashboard(user_id="user-" + role)
                self.assertEqual(set(result["sections"]), sections)
                self.assertEqual(set(result["operator"]["sections"]), sections)

    def test_unknown_operator_and_invalid_limit_are_rejected(self) -> None:
        store = InMemoryOperationsStore()
        service = OperationsService(store)
        with self.assertRaises(PermissionError):
            service.dashboard(user_id="ordinary-user")
        store.grant(user_id="operator", role="operations")
        for limit in (0, 101, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                service.dashboard(user_id="operator", limit=limit)

    def test_schema_and_mysql_adapter_do_not_select_sensitive_content(self) -> None:
        root = Path(__file__).resolve().parents[1]
        migration = (root / "services" / "web_auth" / "migrations" / "0012_operations_admin.sql").read_text(encoding="utf-8")
        adapter = (root / "services" / "web_ops" / "mysql_store.py").read_text(encoding="utf-8")
        self.assertIn("admin_operators", migration)
        self.assertIn("operations_audit_events", migration)
        usage_migration = (root / "services" / "web_domain" / "migrations" / "0013_model_usage_sessions.sql").read_text(encoding="utf-8")
        self.assertIn("model_usage_sessions", usage_migration)
        self.assertNotIn("CONCAT(", adapter)
        for forbidden in ("phone_lookup_hash", "phone_ciphertext", "stem_text", "answer_text", "solution_text", "object_key"):
            self.assertNotIn(forbidden, adapter)


if __name__ == "__main__":
    unittest.main()
