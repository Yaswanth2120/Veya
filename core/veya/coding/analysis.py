"""Deterministic local code diagnostics; no source text is logged."""

from __future__ import annotations

import ast


def analyze_python(source: str) -> dict:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {"syntax_ok": False, "diagnostics": [{"line": exc.lineno or 1, "message": "Python syntax error."}], "complexity": 0}
    branches = sum(isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.BoolOp, ast.IfExp, ast.Match)) for node in ast.walk(tree))
    functions = sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(tree))
    return {"syntax_ok": True, "diagnostics": [], "complexity": 1 + branches, "function_count": functions}
