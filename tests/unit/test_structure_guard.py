"""结构守卫：防止类方法因缩进漂移被错误归属。"""

from __future__ import annotations

import ast
from pathlib import Path


def _load_module_ast() -> ast.Module:
    root = Path(__file__).resolve().parents[2]
    source = (root / "eva.py").read_text(encoding="utf-8")
    return ast.parse(source)


def _class_method_names(tree: ast.Module, class_name: str) -> set[str]:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return set()


def test_tool_registry_owns_core_methods() -> None:
    tree = _load_module_ast()
    methods = _class_method_names(tree, "ToolRegistry")
    expected = {
        "_execute_direct",
        "_audit_log",
        "_run_readonly_cli",
        "_run_mutating_cli",
        "_run_cli",
        "_leave_memory_hints",
        "register",
        "get_schemas",
        "execute",
        "setup_builtin_tools",
    }
    missing = expected - methods
    assert not missing, f"ToolRegistry 缺少方法: {sorted(missing)}"


def test_sandbox_exec_runner_does_not_absorb_tool_registry_methods() -> None:
    tree = _load_module_ast()
    methods = _class_method_names(tree, "SandboxExecRunner")
    forbidden = {
        "_audit_log",
        "_run_readonly_cli",
        "_run_mutating_cli",
        "_run_cli",
        "_leave_memory_hints",
        "register",
        "get_schemas",
        "execute",
        "setup_builtin_tools",
    }
    leaked = forbidden & methods
    assert not leaked, f"SandboxExecRunner 错误吸收方法: {sorted(leaked)}"

