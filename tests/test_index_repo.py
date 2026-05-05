"""End-to-end test: drive `jobs.start_index_repo` against a tiny local repo
and verify the output directory layout matches the plan."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from deco_assaying import config, jobs


@pytest.fixture
def output_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test OUTPUT_ROOT under tmp_path so jobs land in isolation."""
    root = tmp_path / "output"
    monkeypatch.setattr(config, "OUTPUT_ROOT", root)
    return root


def _wait_done(job_id: str, timeout: float = 30.0) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        snap = jobs.get_status(job_id)
        assert snap is not None
        if snap["state"] in ("done", "failed", "cancelled"):
            return snap
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_index_repo_against_local_fixture(tmp_path: Path, output_root: Path):
    src = tmp_path / "src"
    _write(src / "alpha.py", '"""alpha doc."""\n\ndef hello(name): return f"hi {name}"\n')
    _write(src / "pkg" / "beta.py", "class Beta:\n    def m(self): return 1\n")
    _write(src / "main.go", 'package main\n\nimport "fmt"\n\nfunc main() {\n    fmt.Println("hi")\n}\n')
    _write(src / "ts" / "util.ts", 'export const greeting = "hi";\n')
    _write(src / "tests" / "test_alpha.py", "def test_hi(): assert True\n")
    _write(src / "README.md", "# Demo\n")
    _write(src / ".gitignore", "ignored.py\n")
    _write(src / "ignored.py", "# should be skipped\n")
    _write(src / "node_modules" / "leftpad" / "index.js", "export default x => x;\n")

    job_id, output_path = jobs.start_index_repo({"source": str(src)})
    assert output_path.parent == output_root
    assert output_path.name == job_id

    snap = _wait_done(job_id)
    assert snap["state"] == "done", f"job failed: {snap}"
    assert snap["output_path"] == str(output_path)

    # Manifest exists and has the rollup we expect.
    manifest_path = Path(snap["manifest_path"])
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["file_count"] >= 5
    assert "python" in manifest["languages"]
    assert "go" in manifest["languages"]
    assert "typescript" in manifest["languages"]
    assert "main.go" in manifest["entry_points"]
    assert manifest["test_file_count"] >= 1

    # Per-file artifacts mirror the source tree.
    files_dir = output_path / "files"
    assert (files_dir / "alpha.py.json").exists()
    assert (files_dir / "pkg" / "beta.py.json").exists()
    assert (files_dir / "main.go.json").exists()
    # gitignore filtered, node_modules pruned, .git absent.
    assert not (files_dir / "ignored.py.json").exists()
    assert not (files_dir / "node_modules").exists()

    # Per-file artifact has the documented shape.
    alpha = json.loads((files_dir / "alpha.py.json").read_text())
    assert alpha["file"]["language"] == "python"
    assert alpha["module_doc"] == "alpha doc."
    assert any(s["qualified_name"] == "hello" for s in alpha["symbols"])

    # all_symbols.json and languages.json wrote.
    symbols = json.loads((output_path / "all_symbols.json").read_text())
    qnames = {e["qualified_name"] for e in symbols["entries"]}
    assert "hello" in qnames
    assert any(q.startswith("Beta") for q in qnames)

    # top_level_symbols.json is the cheap-view sibling — same shape,
    # methods/nested entries filtered out.
    top_level = json.loads((output_path / "top_level_symbols.json").read_text())
    top_qnames = {e["qualified_name"] for e in top_level["entries"]}
    assert "hello" in top_qnames
    # Methods (e.g. `Beta.m` from pkg/beta.py) are dotted and should be absent.
    assert all("." not in q for q in top_qnames)
    # The filtered view is strictly smaller.
    assert len(top_level["entries"]) <= len(symbols["entries"])

    # analysis_index.json catalogs every artifact with size + URL,
    # including manifest.json and itself (with size_bytes=null for the
    # self-entry to avoid the recursive-size problem).
    index = json.loads((output_path / "analysis_index.json").read_text())
    names = {a["name"] for a in index["artifacts"]}
    assert {
        "manifest.json",
        "all_symbols.json",
        "top_level_symbols.json",
        "tree.json",
        "analysis_index.json",
    } <= names
    assert all(a["size_bytes"] is None or a["size_bytes"] > 0 for a in index["artifacts"])
    assert all(a["url"].startswith("http") for a in index["artifacts"])

    languages = json.loads((output_path / "languages.json").read_text())
    assert "python" in languages["languages"]

    # log.jsonl contains a stream of events.
    log_lines = [json.loads(ln) for ln in (output_path / "log.jsonl").read_text().splitlines() if ln.strip()]
    events = {ev["event"] for ev in log_lines}
    assert "walk_done" in events
    assert "file_done" in events
    assert "manifest_written" in events

    # tree.json lists every path the walker saw — analyzed and skipped.
    tree = json.loads((output_path / "tree.json").read_text())
    by_path = {e["path"]: e for e in tree["entries"]}
    assert by_path["alpha.py"]["analyzed"] is True
    assert "ignored.py" in by_path
    assert by_path["ignored.py"]["analyzed"] is False
    assert by_path["ignored.py"]["skip_reason"] == "gitignore"
    assert not any(p.startswith("node_modules/") for p in by_path)
    assert manifest["tree_total"] == len(by_path)
    assert manifest["skipped_count"] >= 1
    assert manifest["skipped_by_reason"].get("gitignore", 0) >= 1


def test_index_repo_returns_fresh_output_path_per_call(tmp_path: Path, output_root: Path):
    """Two index_repo calls against the same source land in two different
    `OUTPUT_ROOT/{job_id}/` dirs — server allocates a fresh one each time."""
    src = tmp_path / "src"
    _write(src / "x.py", "x = 1\n")

    job_a, path_a = jobs.start_index_repo({"source": str(src)})
    job_b, path_b = jobs.start_index_repo({"source": str(src)})
    _wait_done(job_a)
    _wait_done(job_b)
    assert job_a != job_b
    assert path_a != path_b
    assert path_a.parent == output_root
    assert path_b.parent == output_root


def test_index_repo_rejects_unsafe_url(output_root: Path):
    job_id, _ = jobs.start_index_repo({"source": "git@github.com:foo/bar.git"})
    snap = _wait_done(job_id)
    assert snap["state"] == "failed"
    assert "unsupported" in snap["error"].lower() or "scheme" in snap["error"].lower()
