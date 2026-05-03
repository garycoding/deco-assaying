from parkview_codeparse import analyze


def test_unknown_extension_returns_no_parser_envelope():
    r = analyze.analyze_inline(content="hello world", filename="thing.xyz")
    assert r["file"]["language"] == ""
    assert r["parse"]["ok"] is False
    assert r["parse"].get("reason") == "no_parser"
    assert r["chunks"] == []
    assert r["symbols"] == []


def test_typescript_uses_fallback_analyzer():
    src = "export function add(a: number, b: number) { return a + b; }\n"
    r = analyze.analyze_inline(content=src, filename="util.ts")
    assert r["file"]["language"] == "typescript"
    # Parse succeeds (tree-sitter has typescript), but the fallback analyzer
    # leaves symbols empty until a TS-specific analyzer is added.
    assert r["parse"]["ok"] is True
    assert r["symbols"] == []
    assert r["chunks"]  # chunking is language-agnostic


def test_string_literals_extracted_in_typescript():
    src = 'const url = "https://example.com/x";\n'
    r = analyze.analyze_inline(content=src, filename="x.ts")
    urls = [lit for lit in r["literals_of_interest"] if lit["kind"] == "url"]
    assert any(u["value"] == "https://example.com/x" for u in urls)
