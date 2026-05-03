"""End-to-end tests for the Python analyzer."""

from parkview_codeparse import analyze

_SOURCE = '''"""Top-level module docstring."""
import os
from pathlib import Path as P
from .util import x, y

__all__ = ["Foo", "top"]

CONSTANT: int = 42

class Foo(Bar):
    """Foo doc."""

    field_a: int = 1

    @classmethod
    async def baz(cls, n: int) -> str:
        """Baz doc."""
        url = "https://api.example.com/v1/items"
        return str(n)

    def __init__(self):
        self.x = 1


def top(*args, **kw):
    """Generator."""
    yield 1


def test_helper():
    assert True


if __name__ == "__main__":
    top()
'''


def test_file_metadata():
    r = analyze.analyze_inline(content=_SOURCE, filename="example.py")
    f = r["file"]
    assert f["language"] == "python"
    assert f["bytes"] == len(_SOURCE.encode())
    assert f["loc"] > 0
    assert f["sha256"]
    assert not f["is_test"]
    assert not f["is_generated"]


def test_module_docstring():
    r = analyze.analyze_inline(content=_SOURCE, filename="example.py")
    assert r["module_doc"] == "Top-level module docstring."


def test_imports():
    r = analyze.analyze_inline(content=_SOURCE, filename="example.py")
    by_module = {imp["module"]: imp for imp in r["imports"]}
    assert "os" in by_module
    assert by_module["os"]["kind"] == "import"
    assert "pathlib.Path" in by_module
    assert by_module["pathlib.Path"]["alias"] == "P"
    assert ".util.x" in by_module
    assert ".util.y" in by_module


def test_exports_from_dunder_all():
    r = analyze.analyze_inline(content=_SOURCE, filename="example.py")
    names = {e["name"] for e in r["exports"]}
    assert names == {"Foo", "top"}


def test_symbols_kinds_and_qnames():
    r = analyze.analyze_inline(content=_SOURCE, filename="example.py")
    by_qname = {s["qualified_name"]: s for s in r["symbols"]}
    assert by_qname["CONSTANT"]["kind"] == "constant"
    assert by_qname["Foo"]["kind"] == "class"
    assert by_qname["Foo"]["signature"] == "class Foo(Bar)"
    assert by_qname["Foo"]["doc"] == "Foo doc."
    assert by_qname["Foo.field_a"]["kind"] == "field"
    assert by_qname["Foo.baz"]["kind"] == "method"
    assert "classmethod" in by_qname["Foo.baz"]["modifiers"]
    assert "async" in by_qname["Foo.baz"]["modifiers"]
    assert by_qname["Foo.__init__"]["kind"] == "constructor"
    assert by_qname["top"]["kind"] == "function"
    assert "generator" in by_qname["top"]["modifiers"]


def test_references():
    r = analyze.analyze_inline(content=_SOURCE, filename="example.py")
    inherits = [ref for ref in r["references"] if ref["kind"] == "inherit"]
    assert any(ref["qualifier"] == "Bar" for ref in inherits)

    calls = [ref for ref in r["references"] if ref["kind"] == "call"]
    call_names = {ref["name"] for ref in calls}
    # `top()` inside the main guard, `str()` inside Foo.baz
    assert "top" in call_names
    assert "str" in call_names


def test_metrics():
    r = analyze.analyze_inline(content=_SOURCE, filename="example.py")
    m = r["metrics"]
    assert m["n_classes"] == 1
    assert m["has_main_guard"]
    assert m["async_count"] == 1
    assert m["generator_count"] == 1
    assert m["test_count"] == 1


def test_literals_includes_url_only_once():
    r = analyze.analyze_inline(content=_SOURCE, filename="example.py")
    urls = [lit for lit in r["literals_of_interest"] if lit["kind"] == "url"]
    assert len(urls) == 1
    assert urls[0]["value"] == "https://api.example.com/v1/items"


def test_parse_ok():
    r = analyze.analyze_inline(content=_SOURCE, filename="example.py")
    assert r["parse"] == {"ok": True, "error_nodes": 0, "missing_nodes": 0}


def test_broken_syntax_reports_error_but_recovers_symbols():
    broken = "def good():\n    return 1\n\ndef bad(:\n    return 2\n\ndef good2():\n    return 3\n"
    r = analyze.analyze_inline(content=broken, filename="broken.py")
    assert r["parse"]["ok"] is False
    # Tree-sitter recovery may report the breakage as either an ERROR node
    # or a MISSING node depending on grammar — both indicate the parser
    # had to fix something up.
    assert (r["parse"]["error_nodes"] + r["parse"]["missing_nodes"]) >= 1
    names = {s["name"] for s in r["symbols"]}
    assert "good" in names
    assert "good2" in names
