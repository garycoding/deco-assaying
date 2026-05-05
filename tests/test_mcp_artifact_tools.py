"""Tests for the get_* / list_job_files MCP tools — the v0.1.2 surface
that lets a remote LLM read a finished job's artifacts inline.

The MCP transport machinery is the same as test_http_routes.py;
helpers are duplicated minimally to keep this file self-contained.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deco_assaying import config, jobs

# ---------------------------------------------------------------------------
# Fixtures + helpers


@pytest.fixture
def client(mcp_client: TestClient) -> TestClient:
    """Alias to the session-scoped mcp_client from conftest.py."""
    return mcp_client


@pytest.fixture
def output_root(tmp_path, monkeypatch):
    root = tmp_path / "output"
    monkeypatch.setattr(config, "OUTPUT_ROOT", root)
    return root


def _parse_sse(body: str) -> list[dict]:
    out: list[dict] = []
    for line in body.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
        elif line.startswith("data:"):
            out.append(json.loads(line[5:]))
    return out


def _mcp(client, method, params=None, *, req_id=1, session_id=None):
    payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    resp = client.post("/sse", json=payload, headers=headers)
    assert resp.status_code == 200
    ct = resp.headers.get("content-type", "")
    if ct.startswith("application/json"):
        return resp.json(), dict(resp.headers)
    msgs = _parse_sse(resp.text)
    assert msgs
    return msgs[-1], dict(resp.headers)


def _initialize(client) -> str:
    body, headers = _mcp(
        client,
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0.0"},
        },
        req_id=1,
    )
    assert body["result"]["serverInfo"]["name"] == "deco-assaying"
    return headers.get("mcp-session-id", "")


def _call(client, sid, name, args, *, req_id=100):
    body, _ = _mcp(client, "tools/call", {"name": name, "arguments": args}, req_id=req_id, session_id=sid)
    assert "result" in body, f"tools/call returned: {body!r}"
    contents = body["result"]["content"]
    return json.loads(contents[0]["text"])


def _wait_done(job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = jobs.get_status(job_id)
        assert snap is not None
        if snap["state"] in ("done", "failed", "cancelled"):
            return snap
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


@pytest.fixture
def finished_job(tmp_path: Path, output_root: Path) -> tuple[str, Path]:
    """Index a tiny multi-language local repo and return (job_id, out)."""
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "alpha.py").write_text(
        '"""alpha."""\n\ndef hello(): return 1\n\nclass A:\n    def m(self): pass\n'
    )
    (src / "pkg" / "beta.py").write_text("class Beta:\n    def m(self): return 2\n")
    (src / "main.go").write_text("package main\n\nfunc main() {}\n")
    job_id, out = jobs.start_index_repo({"source": str(src)})
    snap = _wait_done(job_id)
    assert snap["state"] == "done"
    return job_id, out


# ---------------------------------------------------------------------------
# get_manifest / get_languages / get_errors — the small rollups


def test_mcp_get_manifest(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "get_manifest", {"job_id": job_id})
    assert payload["job_id"] == job_id
    assert payload["file_count"] >= 3
    assert "python" in payload["languages"]
    assert "go" in payload["languages"]
    # languages_by_count is the same data sorted desc by file_count
    # so the model can read off the dominant language directly.
    by_count = payload["languages_by_count"]
    assert isinstance(by_count, list)
    assert {row["language"] for row in by_count} == set(payload["languages"].keys())
    counts = [row["file_count"] for row in by_count]
    assert counts == sorted(counts, reverse=True)
    # Python has 2 files (alpha.py + pkg/beta.py); Go has 1.
    assert by_count[0]["language"] == "python"


def test_mcp_get_languages(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "get_languages", {"job_id": job_id})
    assert "python" in payload["languages"]
    assert payload["languages"]["python"]["file_count"] >= 2


def test_mcp_get_errors_clean_run(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "get_errors", {"job_id": job_id})
    assert payload["parse_errors"] == []


# ---------------------------------------------------------------------------
# get_tree — full inventory + filters


def test_mcp_get_tree_no_filter(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "get_tree", {"job_id": job_id})
    paths = {e["path"] for e in payload["entries"]}
    assert {"alpha.py", "main.go", "pkg/beta.py"} <= paths
    assert payload["total_returned"] == payload["total_in_repo"]
    # No filter → returned-size matches repo-total-size.
    assert payload["total_size_bytes"] == payload["total_size_bytes_in_repo"]
    assert payload["total_size_bytes"] > 0


def test_mcp_get_tree_path_prefix(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "get_tree", {"job_id": job_id, "path_prefix": "pkg/"})
    paths = {e["path"] for e in payload["entries"]}
    assert paths == {"pkg/beta.py"}
    assert payload["total_returned"] < payload["total_in_repo"]
    # Filtered slice is smaller than the whole tree.
    assert 0 < payload["total_size_bytes"] < payload["total_size_bytes_in_repo"]


def test_mcp_get_tree_analyzed_only(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "get_tree", {"job_id": job_id, "analyzed_only": True})
    assert all(e.get("analyzed") for e in payload["entries"])


# ---------------------------------------------------------------------------
# get_all_symbols + get_top_level_symbols


def test_mcp_get_all_symbols_no_filter(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "get_all_symbols", {"job_id": job_id})
    qnames = {e["qualified_name"] for e in payload["entries"]}
    assert "hello" in qnames
    assert any(q.startswith("Beta") for q in qnames)


def test_mcp_get_all_symbols_kind_filter(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "get_all_symbols", {"job_id": job_id, "kind": "class"})
    assert all(e["kind"] == "class" for e in payload["entries"])
    assert payload["total_returned"] < payload["total_in_repo"]


def test_mcp_get_all_symbols_file_prefix(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "get_all_symbols", {"job_id": job_id, "file_prefix": "pkg/"})
    assert all(e["file"].startswith("pkg/") for e in payload["entries"])


def test_mcp_get_top_level_symbols(client, finished_job):
    """Top-level view: no dotted qualified_names, no module-kind entries.
    `hello` (top-level fn in alpha.py) shows up; `Beta.m` (method) does not."""
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "get_top_level_symbols", {"job_id": job_id})
    qnames = {e["qualified_name"] for e in payload["entries"]}
    assert "hello" in qnames
    assert all("." not in q for q in qnames)
    assert all(e.get("kind") != "module" for e in payload["entries"])


def test_mcp_get_top_level_symbols_smaller_than_all(client, finished_job):
    """The cheap-view file is strictly smaller than the all-symbols file."""
    sid = _initialize(client)
    job_id, _ = finished_job
    top = _call(client, sid, "get_top_level_symbols", {"job_id": job_id})
    full = _call(client, sid, "get_all_symbols", {"job_id": job_id})
    assert top["total_in_repo"] <= full["total_in_repo"]


def test_mcp_get_top_level_symbols_filters(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(
        client,
        sid,
        "get_top_level_symbols",
        {"job_id": job_id, "file_prefix": "pkg/"},
    )
    assert all(e["file"].startswith("pkg/") for e in payload["entries"])


# ---------------------------------------------------------------------------
# get_analysis_index


def test_mcp_get_analysis_index(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "get_analysis_index", {"job_id": job_id})
    assert payload["job_id"] == job_id
    assert payload["public_base_url"].startswith("http")
    names = {a["name"] for a in payload["artifacts"]}
    # Both symbols variants are listed; the agent can compare sizes.
    assert "all_symbols.json" in names
    assert "top_level_symbols.json" in names
    assert "tree.json" in names
    # The index lists manifest.json (consumers iterating artifacts get a complete catalog).
    assert "manifest.json" in names
    # The index lists itself with size_bytes=null (we'd recurse on our own size).
    assert "analysis_index.json" in names
    self_entry = next(a for a in payload["artifacts"] if a["name"] == "analysis_index.json")
    assert self_entry["size_bytes"] is None
    # Per-file artifacts are listed too.
    assert any(a["name"].startswith("files/") for a in payload["artifacts"])
    # Sizes are real (where present) and URLs are absolute.
    assert all(a["size_bytes"] is None or a["size_bytes"] > 0 for a in payload["artifacts"])
    assert all(a["url"].startswith(payload["public_base_url"]) for a in payload["artifacts"])
    # Bundle ZIP entry.
    assert payload["bundle"]["url"].endswith("/zip")
    # total_size_bytes excludes the self-entry (which is null).
    assert payload["total_size_bytes"] > 0


def test_mcp_analysis_index_top_level_smaller_than_all_on_disk(client, finished_job):
    """The on-disk top_level_symbols.json is smaller than all_symbols.json
    — caller can read sizes directly from the index without downloading."""
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "get_analysis_index", {"job_id": job_id})
    by_name = {a["name"]: a for a in payload["artifacts"]}
    assert by_name["top_level_symbols.json"]["size_bytes"] <= by_name["all_symbols.json"]["size_bytes"]


# ---------------------------------------------------------------------------
# list_job_files + get_file_analysis


def test_mcp_list_job_files_all(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "list_job_files", {"job_id": job_id})
    assert "alpha.py" in payload["paths"]
    assert "pkg/beta.py" in payload["paths"]
    assert payload["count"] == len(payload["paths"])


def test_mcp_list_job_files_glob(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "list_job_files", {"job_id": job_id, "glob": "pkg/*.py"})
    assert payload["paths"] == ["pkg/beta.py"]


def test_mcp_get_file_analysis_full(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "get_file_analysis", {"job_id": job_id, "path": "alpha.py"})
    assert payload["file"]["language"] == "python"
    assert payload["module_doc"] == "alpha."
    assert any(s["qualified_name"] == "hello" for s in payload["symbols"])
    assert "chunks" in payload  # full payload includes everything


def test_mcp_get_file_analysis_sections_subset(client, finished_job):
    """Sections filter trims the payload — 'chunks' is the largest piece;
    consumers can drop it to keep token cost down."""
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(
        client,
        sid,
        "get_file_analysis",
        {"job_id": job_id, "path": "alpha.py", "sections": ["symbols", "imports"]},
    )
    assert set(payload.keys()) == {"symbols", "imports"}


def test_mcp_get_file_analysis_missing(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "get_file_analysis", {"job_id": job_id, "path": "nope.py"})
    assert payload["error"] == "artifact_missing"
    assert "nope.py" in payload["detail"]


def test_mcp_get_file_analysis_invalid_section(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(
        client,
        sid,
        "get_file_analysis",
        {"job_id": job_id, "path": "alpha.py", "sections": ["bogus"]},
    )
    assert payload["error"] == "bad_request"


def test_mcp_get_file_analysis_path_traversal(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(
        client,
        sid,
        "get_file_analysis",
        {"job_id": job_id, "path": "../../../etc/passwd"},
    )
    assert payload["error"] in ("bad_request", "artifact_missing")


# ---------------------------------------------------------------------------
# Unknown job_id — every artifact tool short-circuits cleanly


@pytest.mark.parametrize(
    "tool,extra",
    [
        ("get_manifest", {}),
        ("get_tree", {}),
        ("get_all_symbols", {}),
        ("get_top_level_symbols", {}),
        ("get_languages", {}),
        ("get_errors", {}),
        ("get_analysis_index", {}),
        ("list_job_files", {}),
        ("get_file_analysis", {"path": "x.py"}),
        ("get_log_events", {}),
    ],
)
def test_mcp_artifact_tools_unknown_job(client, output_root, tool, extra):
    sid = _initialize(client)
    payload = _call(client, sid, tool, {"job_id": "does-not-exist", **extra})
    assert payload == {"error": "unknown_job_id"}


# ---------------------------------------------------------------------------
# get_log_events


def test_mcp_get_log_events(client, finished_job):
    sid = _initialize(client)
    job_id, _ = finished_job
    payload = _call(client, sid, "get_log_events", {"job_id": job_id})
    assert isinstance(payload["events"], list)
    assert payload["next_offset"] > 0
    events = {e["event"] for e in payload["events"]}
    assert "manifest_written" in events
