"""Per-file analyzer entry point.

Coordinates: language detection, tree-sitter parsing, dispatch to the
language-specific analyzer in `parkview_codeparse.analyzers`, language-
agnostic literal-of-interest extraction over string nodes, AST-aware
chunking, file-level metadata + heuristic detectors, parse-error
reporting.

The output shape is the per-file JSON documented in the plan; both the
inline `analyze_file` MCP tool and the `index_repo` worker call this.
"""

from __future__ import annotations

import hashlib
from typing import Any

from parkview_codeparse import chunks, detectors, languages, literals
from parkview_codeparse.analyzers import get_analyzer
from parkview_codeparse.analyzers._base import count_parse_errors


def analyze_inline(
    *,
    content: str,
    filename: str = "",
    language: str = "",
    include_chunks: bool = True,
    chunk_max_tokens: int = 800,
) -> dict[str, Any]:
    """Analyze source code passed as a string.

    Returns the per-file JSON shape documented in the plan. If the language
    is unknown or has no full-support analyzer, a minimal envelope is still
    returned (file metadata + chunks + parse status) — never raises for
    valid input.
    """
    encoded = content.encode("utf-8", errors="replace")
    sha = hashlib.sha256(encoded).hexdigest()
    lines = content.splitlines()
    blank = sum(1 for ln in lines if not ln.strip())
    first_line = lines[0] if lines else ""

    detected = language or languages.detect(filename, first_line=first_line)

    file_meta = {
        "path": filename,
        "language": detected,
        "sha256": sha,
        "bytes": len(encoded),
        "loc": len(lines),
        "sloc": len(lines) - blank,
        "blank": blank,
        "comment": 0,
        "is_generated": detectors.is_generated(filename, content),
        "is_test": detectors.is_test(filename),
        "is_config": detectors.is_config(filename),
    }

    parser = languages.get_parser(detected) if detected else None
    if parser is None:
        # Either unknown language or grammar unavailable: return a coherent
        # envelope so the caller can still record the file existed.
        return {
            "file": file_meta,
            "module_doc": "",
            "symbols": [],
            "imports": [],
            "exports": [],
            "references": [],
            "literals_of_interest": [],
            "chunks": [],
            "metrics": _empty_metrics(),
            "parse": {"ok": False, "error_nodes": 0, "missing_nodes": 0, "reason": "no_parser"},
        }

    tree = parser.parse(encoded)
    root = tree.root_node

    error_nodes, missing_nodes = count_parse_errors(root)

    analyzer, _has_full = get_analyzer(detected)
    result = analyzer(encoded, root)

    # Comment line count: count each `comment` node's line range.
    file_meta["comment"] = _count_comment_lines(root)

    # Literals of interest: walk every string literal, regardless of
    # language. Language analyzers don't have to know about this.
    result["literals_of_interest"] = _collect_literals(encoded, root)

    if include_chunks:
        symbol_index = _build_symbol_index(result.get("symbols", []))
        result["chunks"] = chunks.chunk_tree(
            root=root,
            source_bytes=encoded,
            max_tokens=chunk_max_tokens,
            qualified_name_for=lambda n: _qname_for_node(n, symbol_index),
        )
    else:
        result["chunks"] = []

    result["file"] = file_meta
    result["parse"] = {
        "ok": error_nodes == 0 and missing_nodes == 0,
        "error_nodes": error_nodes,
        "missing_nodes": missing_nodes,
    }
    return result


# ---------------------------------------------------------------------------
# Helpers


def _empty_metrics() -> dict[str, Any]:
    return {
        "n_functions": 0,
        "n_classes": 0,
        "max_nest_depth": 0,
        "has_main_guard": False,
        "async_count": 0,
        "generator_count": 0,
        "test_count": 0,
    }


def _count_comment_lines(root: Any) -> int:
    seen: set[int] = set()
    stack: list[Any] = [root]
    while stack:
        n = stack.pop()
        if n.type == "comment":
            for ln in range(n.start_point[0], n.end_point[0] + 1):
                seen.add(ln)
        stack.extend(n.children)
    return len(seen)


def _collect_literals(source_bytes: bytes, root: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    stack: list[Any] = [root]
    while stack:
        n = stack.pop()
        # tree-sitter's named string node varies by grammar; the common ones
        # are "string" and "string_literal". Both accumulate `string_content`
        # children we can pluck out.
        if n.type in ("string", "string_literal", "raw_string", "interpreted_string_literal"):
            value = _string_text(source_bytes, n)
            if value:
                ctx_start = max(0, n.start_byte - 64)
                ctx = source_bytes[ctx_start : n.start_byte].decode("utf-8", errors="replace")
                span_dict = {
                    "start_byte": n.start_byte,
                    "end_byte": n.end_byte,
                    "start_line": n.start_point[0] + 1,
                    "end_line": n.end_point[0] + 1,
                }
                out.extend(literals.extract(value, span=span_dict, context_before=ctx))
        stack.extend(n.children)
    return out


def _string_text(source_bytes: bytes, node: Any) -> str:
    parts: list[str] = []
    for c in node.children:
        if c.type == "string_content":
            parts.append(source_bytes[c.start_byte : c.end_byte].decode("utf-8", errors="replace"))
    if parts:
        return "".join(parts)
    raw = source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
    for q in ('"""', "'''", '"', "'", "`"):
        if raw.startswith(q) and raw.endswith(q) and len(raw) >= 2 * len(q):
            return raw[len(q) : -len(q)]
    return raw


def _build_symbol_index(symbols: list[dict[str, Any]]) -> list[tuple[int, int, str, str]]:
    """Sort symbols by (start_byte, -end_byte) so we can pick the deepest
    enclosing one for any node.
    """
    rows: list[tuple[int, int, str, str]] = []
    for s in symbols:
        sp = s["span"]
        rows.append((sp["start_byte"], sp["end_byte"], s["qualified_name"], s["kind"]))
    rows.sort(key=lambda r: (r[0], -r[1]))
    return rows


def _qname_for_node(node: Any, symbol_index: list[tuple[int, int, str, str]]) -> tuple[str, str]:
    """Pick the deepest symbol whose span encloses `node.start_byte`."""
    best: tuple[str, str] | None = None
    sb = node.start_byte
    for start, end, qname, kind in symbol_index:
        if start <= sb < end:
            best = (qname, kind)
        elif start > sb:
            break
    return best or ("", "")
