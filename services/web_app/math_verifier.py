"""A tiny, deterministic math checker with no code-execution or shell surface."""

from __future__ import annotations

import ast
import math
import re
from typing import Any


_VARIABLE = re.compile(r"^[a-z]$")
_SAMPLES = (-2.0, -0.5, 0.75, 2.0, 3.0)


class UnsupportedExpression(ValueError):
    pass


def verify_equations(checks: Any) -> list[dict[str, Any]]:
    """Check at most eight bounded identities; unsupported input is reported, never executed."""
    if not isinstance(checks, list):
        return []
    return [_verify(check) for check in checks[:8] if isinstance(check, dict)]


def _verify(check: dict[str, Any]) -> dict[str, Any]:
    left, right, variables = check.get("left"), check.get("right"), check.get("variables", [])
    public = {"left": left if isinstance(left, str) else "", "right": right if isinstance(right, str) else ""}
    try:
        if not isinstance(left, str) or not isinstance(right, str) or not isinstance(variables, list):
            raise UnsupportedExpression("invalid_check")
        names = tuple(variables)
        if len(names) > 3 or len(names) != len(set(names)) or any(not isinstance(name, str) or not _VARIABLE.fullmatch(name) for name in names):
            raise UnsupportedExpression("invalid_variables")
        left_tree, right_tree = _parse(left, set(names)), _parse(right, set(names))
        samples = [{}] if not names else [
            {name: _SAMPLES[(index + offset) % len(_SAMPLES)] for offset, name in enumerate(names)}
            for index in range(len(_SAMPLES))
        ]
        compared = 0
        for values in samples:
            try:
                left_value = _evaluate(left_tree, values)
                right_value = _evaluate(right_tree, values)
            except (ZeroDivisionError, ValueError, OverflowError):
                continue
            compared += 1
            if not math.isclose(left_value, right_value, rel_tol=1e-9, abs_tol=1e-9):
                return {**public, "status": "conflict", "samples": compared}
        if compared == 0:
            raise UnsupportedExpression("no_valid_sample")
        return {**public, "status": "verified", "samples": compared}
    except (SyntaxError, UnsupportedExpression) as exc:
        return {**public, "status": "unsupported", "reason": str(exc) or "unsupported_expression"}


def _parse(expression: str, variables: set[str]) -> ast.Expression:
    normalized = expression.strip().replace("−", "-").replace("×", "*").replace("÷", "/").replace("^", "**")
    if not normalized or len(normalized) > 200:
        raise UnsupportedExpression("invalid_length")
    tree = ast.parse(normalized, mode="eval")
    nodes = list(ast.walk(tree))
    if len(nodes) > 64:
        raise UnsupportedExpression("expression_too_complex")
    allowed = (
        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Call,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.UAdd, ast.USub, ast.Load,
    )
    if any(not isinstance(node, allowed) for node in nodes):
        raise UnsupportedExpression("unsupported_syntax")
    for node in nodes:
        if isinstance(node, ast.Constant) and (isinstance(node.value, bool) or not isinstance(node.value, (int, float))):
            raise UnsupportedExpression("invalid_constant")
        if isinstance(node, ast.Name) and node.id not in variables | {"sqrt"}:
            raise UnsupportedExpression("unknown_name")
        if isinstance(node, ast.Call) and (
            not isinstance(node.func, ast.Name) or node.func.id != "sqrt" or len(node.args) != 1 or node.keywords
        ):
            raise UnsupportedExpression("unsupported_function")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            if not isinstance(node.right, ast.Constant) or isinstance(node.right.value, bool) or not isinstance(node.right.value, (int, float)) or abs(node.right.value) > 8:
                raise UnsupportedExpression("invalid_exponent")
    return tree


def _evaluate(node: ast.AST, variables: dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, variables)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise UnsupportedExpression("unknown_name")
        return variables[node.id]
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, variables)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.Call):
        return math.sqrt(_evaluate(node.args[0], variables))
    if isinstance(node, ast.BinOp):
        left, right = _evaluate(node.left, variables), _evaluate(node.right, variables)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.Pow):
            value = left ** right
            if isinstance(value, complex):
                raise ValueError("complex result")
            return value
    raise UnsupportedExpression("unsupported_syntax")
