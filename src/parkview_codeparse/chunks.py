"""AST-aware code chunking (cAST-style).

Splits source into chunks that respect AST boundaries: a chunk never crosses
a function/class boundary, and individual top-level constructs are kept
whole when they fit. Bigger constructs are recursively split at their
children.

The algorithm is the greedy split-and-merge from the cAST paper (CMU): walk
the root's children left-to-right, accumulating into a buffer; when adding
the next sibling would exceed `max_tokens`, flush the buffer as one chunk;
if a single child is itself larger than `max_tokens`, recurse into it.

Token estimation is intentionally cheap and approximate (ceil(bytes / 4)) —
the consumer (Cobgrind) re-tokenizes anyway when it embeds.

Each emitted chunk is annotated with the qualified_name and node-kind of
the closest enclosing symbol, so a downstream embedding pipeline can store
"this chunk belongs to module.Foo.bar" without re-parsing the file.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Keep these in sync with the symbol kinds analyze.py emits.
_SYMBOL_NODE_KINDS: dict[str, str] = {
    "function_definition": "function",
    "class_definition": "class",
    "decorated_definition": "decorated",  # rewritten by the caller to the inner kind
}


def estimate_tokens(text: str) -> int:
    return max(1, (len(text.encode("utf-8", errors="replace")) + 3) // 4)


def _node_text(source_bytes: bytes, node: Any) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _node_span(node: Any) -> dict[str, Any]:
    return {
        "start_byte": node.start_byte,
        "end_byte": node.end_byte,
        "start_line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
    }


def chunk_tree(
    *,
    root: Any,
    source_bytes: bytes,
    max_tokens: int,
    qualified_name_for: Callable[[Any], tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Greedy AST-aware chunking.

    `qualified_name_for(node)` should return `(qualified_name, kind)` for the
    closest enclosing symbol; if it returns `("", "")` the chunk falls back
    to `("<module>", "module")`.
    """
    qfn = qualified_name_for or (lambda _n: ("", ""))
    out: list[dict[str, Any]] = []

    def emit(nodes: list[Any]) -> None:
        if not nodes:
            return
        first, last = nodes[0], nodes[-1]
        text = source_bytes[first.start_byte : last.end_byte].decode("utf-8", errors="replace")
        if not text.strip():
            return
        qname, kind = qfn(first)
        if not qname:
            qname, kind = "<module>", "module"
        out.append(
            {
                "qualified_name": qname,
                "kind": kind,
                "span": {
                    "start_byte": first.start_byte,
                    "end_byte": last.end_byte,
                    "start_line": first.start_point[0] + 1,
                    "end_line": last.end_point[0] + 1,
                },
                "text": text,
                "token_estimate": estimate_tokens(text),
            }
        )

    def visit(parent: Any) -> None:
        buf: list[Any] = []
        buf_tokens = 0
        for child in parent.children:
            if not _is_significant(child):
                continue
            child_text = _node_text(source_bytes, child)
            child_tokens = estimate_tokens(child_text)

            if child_tokens > max_tokens and child.child_count > 1:
                # Flush buffer, then descend.
                emit(buf)
                buf, buf_tokens = [], 0
                visit(child)
                continue

            if buf_tokens + child_tokens > max_tokens and buf:
                emit(buf)
                buf, buf_tokens = [], 0

            buf.append(child)
            buf_tokens += child_tokens

        emit(buf)

    visit(root)
    return out


def _is_significant(node: Any) -> bool:
    """Skip purely-syntactic nodes (commas, parens, keywords) at the
    chunking layer. We still keep their text via the byte range of the
    parent chunk; they just don't get to be chunks themselves.
    """
    return bool(node.is_named)
