"""Python analyzer.

Walks the tree-sitter AST and emits the per-file JSON shape documented in
the plan: symbols (modules/classes/functions/methods/constants), imports
(plain, from-import, relative), exports (`__all__` entries), outgoing
references (calls, type refs, inheritance), metrics (counts + main-guard +
async/generator), and module/class/function docstrings.

Notes for the next reader:

- Tree-sitter's Python grammar represents a top-of-module / top-of-class /
  top-of-function string literal as a `string` node, not wrapped in
  `expression_statement`. We treat the first such `string` child of a body
  as the docstring.
- `decorated_definition` wraps a `function_definition` or `class_definition`;
  the decorators are siblings of the inner definition. We unwrap and record
  modifiers.
- `qualified_name` is dotted from the module root: a method `m` on class
  `Foo` is `Foo.m`; a method on a nested class `Foo.Bar.m` is `Foo.Bar.m`.
- We don't follow imports; references contain the literal qualifier strings
  the source uses. Cross-file resolution is the consumer's job once it has
  every file.
"""

from __future__ import annotations

from typing import Any

from deco_assaying.analyzers._base import empty_result, span, text


def analyze(source_bytes: bytes, root: Any) -> dict[str, Any]:
    out = empty_result()

    # --- module docstring -------------------------------------------------
    out["module_doc"] = _docstring(source_bytes, root)

    # --- top-level walk ---------------------------------------------------
    state = _State(source_bytes)
    state.walk_module(root)

    out["symbols"] = state.symbols
    out["imports"] = state.imports
    out["exports"] = state.exports
    out["references"] = state.references
    out["metrics"] = state.metrics

    return out


# ---------------------------------------------------------------------------
# Internals


class _State:
    def __init__(self, source_bytes: bytes) -> None:
        self.src = source_bytes
        self.symbols: list[dict[str, Any]] = []
        self.imports: list[dict[str, Any]] = []
        self.exports: list[dict[str, Any]] = []
        self.references: list[dict[str, Any]] = []
        self.metrics = {
            "n_functions": 0,
            "n_classes": 0,
            "max_nest_depth": 0,
            "has_main_guard": False,
            "async_count": 0,
            "generator_count": 0,
            "test_count": 0,
        }

    # --- module-level dispatch -------------------------------------------

    def walk_module(self, root: Any) -> None:
        for child in root.children:
            self._handle_top_level(child, parent_qname="", depth=0)

    def _handle_top_level(self, node: Any, *, parent_qname: str, depth: int) -> None:
        t = node.type
        if t == "import_statement":
            self._collect_import(node)
        elif t == "import_from_statement":
            self._collect_from_import(node)
        elif t == "class_definition":
            self._collect_class(node, parent_qname=parent_qname, depth=depth, decorators=())
        elif t == "function_definition":
            self._collect_function(node, parent_qname=parent_qname, depth=depth, decorators=())
        elif t == "decorated_definition":
            inner = node.child_by_field_name("definition")
            decorators = tuple(self._decorator_name(d) for d in node.children if d.type == "decorator")
            if inner is None:
                return
            if inner.type == "class_definition":
                self._collect_class(
                    inner, parent_qname=parent_qname, depth=depth, decorators=decorators, doc_node=node
                )
            elif inner.type == "function_definition":
                self._collect_function(
                    inner, parent_qname=parent_qname, depth=depth, decorators=decorators, doc_node=node
                )
        elif t == "expression_statement":
            self._collect_dunder_all(node)
            self._collect_call_refs(node, in_symbol=parent_qname or "<module>")
        elif t == "assignment":
            # Top-level `__all__ = [...]` and constants both land here.
            self._collect_dunder_all_assignment(node)
            self._collect_module_constant(node)
            self._collect_call_refs(node, in_symbol=parent_qname or "<module>")
        elif t == "if_statement":
            if _is_main_guard(node):
                self.metrics["has_main_guard"] = True
            self._collect_call_refs(node, in_symbol=parent_qname or "<module>")
        else:
            self._collect_call_refs(node, in_symbol=parent_qname or "<module>")

    # --- symbols ----------------------------------------------------------

    def _collect_class(
        self,
        node: Any,
        *,
        parent_qname: str,
        depth: int,
        decorators: tuple[str, ...],
        doc_node: Any | None = None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = text(self.src, name_node)
        qname = f"{parent_qname}.{name}" if parent_qname else name

        body = node.child_by_field_name("body")
        bases = []
        sup = node.child_by_field_name("superclasses")
        if sup is not None:
            for c in sup.children:
                if c.is_named:
                    base_text = text(self.src, c).strip()
                    if base_text:
                        bases.append(base_text)
                        # Inheritance edges are references.
                        self.references.append(
                            {
                                "name": _last_qualifier_segment(base_text),
                                "qualifier": base_text,
                                "kind": "inherit",
                                "span": span(c),
                                "in_symbol": qname,
                            }
                        )

        self.symbols.append(
            {
                "kind": "class",
                "name": name,
                "qualified_name": qname,
                "signature": _class_signature(name, bases),
                "span": span(doc_node or node),
                "doc": _docstring(self.src, body) if body is not None else "",
                "modifiers": list(decorators),
                "parent_qname": parent_qname,
            }
        )
        self.metrics["n_classes"] += 1
        self.metrics["max_nest_depth"] = max(self.metrics["max_nest_depth"], depth + 1)

        if body is not None:
            for child in body.children:
                self._handle_class_body(child, parent_qname=qname, depth=depth + 1)

    def _handle_class_body(self, node: Any, *, parent_qname: str, depth: int) -> None:
        t = node.type
        if t == "function_definition":
            self._collect_function(
                node, parent_qname=parent_qname, depth=depth, decorators=(), is_method=True
            )
        elif t == "decorated_definition":
            inner = node.child_by_field_name("definition")
            decorators = tuple(self._decorator_name(d) for d in node.children if d.type == "decorator")
            if inner is None:
                return
            if inner.type == "function_definition":
                self._collect_function(
                    inner,
                    parent_qname=parent_qname,
                    depth=depth,
                    decorators=decorators,
                    is_method=True,
                    doc_node=node,
                )
            elif inner.type == "class_definition":
                self._collect_class(
                    inner,
                    parent_qname=parent_qname,
                    depth=depth,
                    decorators=decorators,
                    doc_node=node,
                )
        elif t == "class_definition":
            self._collect_class(node, parent_qname=parent_qname, depth=depth, decorators=())
        elif t == "assignment":
            self._collect_class_field(node, parent_qname=parent_qname)
            self._collect_call_refs(node, in_symbol=parent_qname)
        else:
            self._collect_call_refs(node, in_symbol=parent_qname)

    def _collect_function(
        self,
        node: Any,
        *,
        parent_qname: str,
        depth: int,
        decorators: tuple[str, ...],
        is_method: bool = False,
        doc_node: Any | None = None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = text(self.src, name_node)
        qname = f"{parent_qname}.{name}" if parent_qname else name

        params_node = node.child_by_field_name("parameters")
        ret_node = node.child_by_field_name("return_type")
        body = node.child_by_field_name("body")

        is_async = any(c.type == "async" for c in node.children)
        if is_async:
            self.metrics["async_count"] += 1
        is_gen = body is not None and _contains_yield(body)
        if is_gen:
            self.metrics["generator_count"] += 1
        if name.startswith("test_") or name == "setUp" or name == "tearDown":
            self.metrics["test_count"] += 1

        kind = "method" if is_method else "function"
        if "staticmethod" in decorators:
            kind = "method"
        if "classmethod" in decorators:
            kind = "method"
        if name == "__init__" and is_method:
            kind = "constructor"
        if "property" in decorators:
            kind = "property"

        signature = _function_signature(
            name=name,
            params=text(self.src, params_node) if params_node is not None else "()",
            ret=text(self.src, ret_node) if ret_node is not None else "",
            is_async=is_async,
        )

        self.symbols.append(
            {
                "kind": kind,
                "name": name,
                "qualified_name": qname,
                "signature": signature,
                "span": span(doc_node or node),
                "doc": _docstring(self.src, body) if body is not None else "",
                "modifiers": list(decorators)
                + (["async"] if is_async else [])
                + (["generator"] if is_gen else []),
                "parent_qname": parent_qname,
            }
        )
        self.metrics["n_functions"] += 1
        self.metrics["max_nest_depth"] = max(self.metrics["max_nest_depth"], depth + 1)

        if body is not None:
            for child in body.children:
                self._handle_function_body(child, parent_qname=qname, depth=depth + 1)

    def _handle_function_body(self, node: Any, *, parent_qname: str, depth: int) -> None:
        t = node.type
        # Nested defs become symbols too.
        if t == "function_definition":
            self._collect_function(node, parent_qname=parent_qname, depth=depth, decorators=())
        elif t == "decorated_definition":
            inner = node.child_by_field_name("definition")
            decorators = tuple(self._decorator_name(d) for d in node.children if d.type == "decorator")
            if inner is not None and inner.type == "function_definition":
                self._collect_function(
                    inner,
                    parent_qname=parent_qname,
                    depth=depth,
                    decorators=decorators,
                    doc_node=node,
                )
            elif inner is not None and inner.type == "class_definition":
                self._collect_class(
                    inner,
                    parent_qname=parent_qname,
                    depth=depth,
                    decorators=decorators,
                    doc_node=node,
                )
        elif t == "class_definition":
            self._collect_class(node, parent_qname=parent_qname, depth=depth, decorators=())
        else:
            self._collect_call_refs(node, in_symbol=parent_qname)

    # --- module constants & class fields ---------------------------------

    def _collect_module_constant(self, node: Any) -> None:
        target = node.children[0] if node.children else None
        if target is None or target.type != "identifier":
            return
        name = text(self.src, target)
        if not name.isupper() and not (len(name) > 1 and name[0].isupper() and "_" in name):
            # Constant convention: ALL_CAPS or At_Least_Mixed; skip lower_snake.
            return
        self.symbols.append(
            {
                "kind": "constant",
                "name": name,
                "qualified_name": name,
                "signature": text(self.src, node).strip(),
                "span": span(node),
                "doc": "",
                "modifiers": [],
                "parent_qname": "",
            }
        )

    def _collect_class_field(self, node: Any, *, parent_qname: str) -> None:
        target = node.children[0] if node.children else None
        if target is None or target.type != "identifier":
            return
        name = text(self.src, target)
        self.symbols.append(
            {
                "kind": "field",
                "name": name,
                "qualified_name": f"{parent_qname}.{name}",
                "signature": text(self.src, node).strip(),
                "span": span(node),
                "doc": "",
                "modifiers": [],
                "parent_qname": parent_qname,
            }
        )

    # --- imports ---------------------------------------------------------

    def _collect_import(self, node: Any) -> None:
        # `import a, b as c, d.e`
        for c in node.children:
            if c.type == "dotted_name":
                module = text(self.src, c)
                self.imports.append(
                    {
                        "module": module,
                        "alias": None,
                        "kind": "import",
                        "span": span(c),
                    }
                )
            elif c.type == "aliased_import":
                name_node = c.child_by_field_name("name")
                alias_node = c.child_by_field_name("alias")
                module = text(self.src, name_node) if name_node is not None else text(self.src, c)
                alias = text(self.src, alias_node) if alias_node is not None else None
                self.imports.append(
                    {
                        "module": module,
                        "alias": alias,
                        "kind": "import",
                        "span": span(c),
                    }
                )

    def _collect_from_import(self, node: Any) -> None:
        module = ""
        # Modules can be `dotted_name` or `relative_import` (`.foo`, `..bar`).
        for c in node.children:
            if c.type == "dotted_name":
                module = text(self.src, c)
                break
            if c.type == "relative_import":
                module = text(self.src, c)
                break
        kind = "from"
        if module.startswith("."):
            kind = "from"  # relative is still "from"; record as-is

        # The names imported live as siblings after the `import` keyword.
        seen_import_kw = False
        for c in node.children:
            if c.type == "import":
                seen_import_kw = True
                continue
            if not seen_import_kw:
                continue
            if c.type == "dotted_name":
                name = text(self.src, c)
                self.imports.append(
                    {
                        "module": _join_module(module, name),
                        "alias": None,
                        "kind": kind,
                        "span": span(c),
                    }
                )
            elif c.type == "aliased_import":
                name_node = c.child_by_field_name("name")
                alias_node = c.child_by_field_name("alias")
                base = text(self.src, name_node) if name_node is not None else text(self.src, c)
                alias = text(self.src, alias_node) if alias_node is not None else None
                self.imports.append(
                    {
                        "module": _join_module(module, base),
                        "alias": alias,
                        "kind": kind,
                        "span": span(c),
                    }
                )

    # --- __all__ exports -------------------------------------------------

    def _collect_dunder_all(self, expr_stmt: Any) -> None:
        # `__all__ = [...]` may be wrapped in expression_statement on some
        # tree-sitter Python versions.
        if not expr_stmt.children:
            return
        inner = expr_stmt.children[0]
        if inner.type == "assignment":
            self._collect_dunder_all_assignment(inner)

    def _collect_dunder_all_assignment(self, assignment: Any) -> None:
        target = assignment.children[0] if assignment.children else None
        if target is None or text(self.src, target) != "__all__":
            return
        for c in assignment.children:
            if c.type in ("list", "tuple"):
                for item in c.children:
                    if item.type == "string":
                        value = _string_value(self.src, item)
                        if value:
                            self.exports.append({"name": value, "qualified_name": value})

    # --- references ------------------------------------------------------

    def _collect_call_refs(self, node: Any, *, in_symbol: str) -> None:
        """Walk arbitrary subtree, recording call/attribute references."""
        stack: list[Any] = [node]
        while stack:
            n = stack.pop()
            t = n.type
            if t == "call":
                fn = n.child_by_field_name("function")
                if fn is not None:
                    qualifier = text(self.src, fn).strip()
                    name = _last_qualifier_segment(qualifier)
                    if name:
                        self.references.append(
                            {
                                "name": name,
                                "qualifier": qualifier,
                                "kind": "call",
                                "span": span(fn),
                                "in_symbol": in_symbol,
                            }
                        )
            # Don't descend into nested function/class bodies; their refs are
            # collected by the corresponding _handle_*_body walker.
            if t in ("function_definition", "class_definition", "decorated_definition"):
                continue
            stack.extend(n.children)

    # --- decorator name helper -------------------------------------------

    def _decorator_name(self, decorator_node: Any) -> str:
        for c in decorator_node.children:
            if c.is_named:
                if c.type == "call":
                    fn = c.child_by_field_name("function")
                    if fn is not None:
                        return text(self.src, fn).split(".")[-1]
                return text(self.src, c).split(".")[-1]
        return ""


# ---------------------------------------------------------------------------
# Free helpers


def _docstring(source_bytes: bytes, body_node: Any | None) -> str:
    """Return the first leading string literal in `body_node` (or root)."""
    if body_node is None:
        return ""
    for c in body_node.children:
        if not c.is_named:
            continue
        if c.type == "string":
            return _string_value(source_bytes, c).strip()
        if c.type == "expression_statement" and c.children:
            inner = c.children[0]
            if inner.type == "string":
                return _string_value(source_bytes, inner).strip()
        # Stop at the first non-string named node.
        return ""
    return ""


def _string_value(source_bytes: bytes, string_node: Any) -> str:
    """Concatenate `string_content` children, dropping quotes."""
    parts: list[str] = []
    for c in string_node.children:
        if c.type == "string_content":
            parts.append(text(source_bytes, c))
    if parts:
        return "".join(parts)
    raw = text(source_bytes, string_node)
    # Strip leading/trailing matching quotes, including triples.
    for q in ('"""', "'''", '"', "'"):
        if raw.startswith(q) and raw.endswith(q) and len(raw) >= 2 * len(q):
            return raw[len(q) : -len(q)]
    return raw


def _is_main_guard(if_node: Any) -> bool:
    cond = if_node.child_by_field_name("condition")
    if cond is None:
        # Fall back to first comparison_operator child.
        for c in if_node.children:
            if c.type == "comparison_operator":
                cond = c
                break
    if cond is None or cond.type != "comparison_operator":
        return False
    txt = b"".join(bytes(c.text or b"") for c in cond.children)
    raw = txt.decode("utf-8", errors="replace")
    return "__name__" in raw and ("'__main__'" in raw or '"__main__"' in raw)


def _contains_yield(node: Any) -> bool:
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("yield",):
            return True
        # Don't cross into nested function defs.
        if n.type in ("function_definition", "class_definition", "decorated_definition"):
            continue
        stack.extend(n.children)
    return False


def _join_module(module: str, name: str) -> str:
    """Concatenate `from MODULE import NAME` into a single dotted form.

    Relative imports keep their leading dots: `from .util import x` becomes
    `.util.x` (not `.utilx`).
    """
    if not module:
        return name
    return f"{module}.{name}"


def _last_qualifier_segment(qualifier: str) -> str:
    if not qualifier:
        return ""
    return qualifier.split(".")[-1].split("(")[0].strip()


def _function_signature(*, name: str, params: str, ret: str, is_async: bool) -> str:
    prefix = "async def " if is_async else "def "
    suffix = f" -> {ret}" if ret else ""
    return f"{prefix}{name}{params}{suffix}"


def _class_signature(name: str, bases: list[str]) -> str:
    if bases:
        return f"class {name}({', '.join(bases)})"
    return f"class {name}"
