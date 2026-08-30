from __future__ import annotations

import unittest

from services.web_app.math_verifier import MathVerificationFilter, verify_equations


class MathVerifierTests(unittest.TestCase):
    def test_verifies_numeric_and_bounded_symbolic_identities(self) -> None:
        values = verify_equations([
            {"left": "sqrt(2)**2", "right": "2", "variables": []},
            {"left": "(x+1)**2", "right": "x**2+2*x+1", "variables": ["x"]},
            {"left": "x+1", "right": "x+2", "variables": ["x"]},
        ])
        self.assertEqual([value["status"] for value in values], ["verified", "verified", "conflict"])

    def test_rejects_code_execution_and_unbounded_expressions(self) -> None:
        values = verify_equations([
            {"left": "__import__('os').system('whoami')", "right": "0", "variables": []},
            {"left": "x**100", "right": "x", "variables": ["x"]},
            {"left": "open(1)", "right": "1", "variables": []},
        ])
        self.assertEqual([value["status"] for value in values], ["unsupported"] * 3)

    def test_filter_exposes_the_same_provider_independent_result(self) -> None:
        checks = [{"left": "(x+1)**2", "right": "x**2+2*x+1", "variables": ["x"]}]
        self.assertEqual(MathVerificationFilter().verify(checks), verify_equations(checks))


if __name__ == "__main__":
    unittest.main()
