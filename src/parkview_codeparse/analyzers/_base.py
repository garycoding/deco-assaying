"""Shared helpers used by per-language analyzers."""

from __future__ import annotations

from typing import Any


def span(node: Any) -> dict[str, Any]:
    return {
        "start_byte": node.start_byte,
        "end_byte": node.end_byte,
        "start_line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
    }


def text(source_bytes: bytes, node: Any) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def count_parse_errors(root: Any) -> tuple[int, int]:
    """Return (error_nodes, missing_nodes) counted across the whole tree."""
    errors = 0
    missing = 0
    stack: list[Any] = [root]
    while stack:
        n = stack.pop()
        if n.is_error:
            errors += 1
        if n.is_missing:
            missing += 1
        stack.extend(n.children)
    return errors, missing


def empty_result() -> dict[str, Any]:
    """Minimal scaffold that every analyzer fills in."""
    return {
        "module_doc": "",
        "symbols": [],
        "imports": [],
        "exports": [],
        "references": [],
        "literals_of_interest": [],
        "chunks": [],
        "metrics": {
            "n_functions": 0,
            "n_classes": 0,
            "max_nest_depth": 0,
            "has_main_guard": False,
            "async_count": 0,
            "generator_count": 0,
            "test_count": 0,
        },
    }
