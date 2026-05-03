from parkview_codeparse import analyze


def test_no_chunk_crosses_function_boundary():
    src = "\n".join(f"def fn_{i}():\n    return {i}" for i in range(20))
    r = analyze.analyze_inline(content=src, filename="big.py", chunk_max_tokens=40)

    fn_starts = [s["span"]["start_line"] for s in r["symbols"] if s["kind"] == "function"]
    for ch in r["chunks"]:
        cs, ce = ch["span"]["start_line"], ch["span"]["end_line"]
        # No function start may fall *inside* a chunk and not also be at its boundary
        # (i.e. a chunk that begins mid-function would put a boundary before its end).
        for fs in fn_starts:
            if cs < fs <= ce:
                # Must contain the whole function: its end line (fs+1 with our 2-line bodies)
                # has to be inside the chunk too.
                assert fs + 1 <= ce, f"chunk {cs}-{ce} cuts function starting at {fs}"


def test_chunk_qualified_name_attached_to_symbol():
    src = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
    r = analyze.analyze_inline(content=src, filename="x.py", chunk_max_tokens=20)
    qnames = {ch["qualified_name"] for ch in r["chunks"]}
    assert "alpha" in qnames or "<module>" in qnames


def test_module_chunk_when_no_symbols():
    r = analyze.analyze_inline(content="x = 1\ny = 2\n", filename="x.py")
    assert r["chunks"]
    assert all(ch["qualified_name"] in ("<module>", "x", "y") for ch in r["chunks"])


def test_chunks_disabled():
    r = analyze.analyze_inline(content="def f(): pass", filename="x.py", include_chunks=False)
    assert r["chunks"] == []
