"""HTTP-route integration tests.

Drive the FastAPI app via `TestClient` (in-process, no uvicorn), exercising:

- The admin / health endpoints directly.
- The MCP `/sse` Streamable HTTP transport — JSON-RPC 2.0 requests with
  Accept: text/event-stream, responses come back as SSE `data:` lines.

We test through the wire protocol so a regression in the MCP wiring
(payload shape, dispatch, error mapping) gets caught here rather than
slipping past the unit tests.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures + helpers


@pytest.fixture
def client(mcp_client: TestClient) -> TestClient:
    """Alias to the session-scoped mcp_client from conftest.py — keeps
    this module's existing test signatures working."""
    return mcp_client


@pytest.fixture
def output_root(tmp_path, monkeypatch):
    """Per-test OUTPUT_ROOT under tmp_path so jobs don't share state."""
    from deco_assaying import config

    root = tmp_path / "output"
    monkeypatch.setattr(config, "OUTPUT_ROOT", root)
    return root


def _parse_sse(body: str) -> list[dict]:
    """Pull `data: <json>` payloads out of a Streamable HTTP SSE response."""
    out: list[dict] = []
    for line in body.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
        elif line.startswith("data:"):  # tolerate no-space variant
            out.append(json.loads(line[5:]))
    return out


def _mcp(
    client: TestClient,
    method: str,
    params: dict | None = None,
    *,
    req_id: int = 1,
    session_id: str | None = None,
) -> tuple[dict, dict]:
    """Send a JSON-RPC request to /sse and return (response_json, response_headers)."""
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
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text!r}"
    ct = resp.headers.get("content-type", "")
    if ct.startswith("application/json"):
        return resp.json(), dict(resp.headers)
    msgs = _parse_sse(resp.text)
    assert msgs, f"no SSE data lines in {resp.text!r}"
    return msgs[-1], dict(resp.headers)


def _initialize(client: TestClient) -> str:
    """Run the MCP initialize handshake; return the session id."""
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
    assert body.get("result", {}).get("serverInfo", {}).get("name") == "deco-assaying"
    sid = headers.get("mcp-session-id", "")
    return sid


def _call_tool(
    client: TestClient,
    session_id: str,
    name: str,
    arguments: dict,
    *,
    req_id: int = 100,
) -> dict:
    """Run tools/call and parse the (single) text content the server returns.

    Our handlers always emit one TextContent whose body is JSON; this helper
    pulls that JSON out for the test assertion.
    """
    body, _ = _mcp(
        client,
        "tools/call",
        {"name": name, "arguments": arguments},
        req_id=req_id,
        session_id=session_id,
    )
    assert "result" in body, f"tools/call returned: {body!r}"
    contents = body["result"]["content"]
    assert contents and contents[0]["type"] == "text"
    return json.loads(contents[0]["text"])


# ---------------------------------------------------------------------------
# /health and /admin/* endpoints


def test_health(client: TestClient):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"]
    assert body["uptime_seconds"] >= 0


def test_admin_version(client: TestClient):
    r = client.get("/admin/version")
    assert r.status_code == 200
    body = r.json()
    assert body["version"]
    # Real package versions, not "unknown" — see review punch list #4.
    assert body["mcp_protocol_version"] != "unknown"
    assert body["tree_sitter_language_pack_version"] != "unknown"


def test_admin_languages(client: TestClient):
    r = client.get("/admin/languages")
    assert r.status_code == 200
    items = r.json()
    ids = {row["id"] for row in items}
    # Every fully-supported language is reported as such.
    fully = {row["id"] for row in items if row["has_full_support"]}
    assert {
        "python",
        "typescript",
        "javascript",
        "go",
        "rust",
        "java",
        "ruby",
        "c",
        "cpp",
        "csharp",
        "php",
        "bash",
    } <= fully
    assert "yaml" in ids  # unsupported but still listed


def test_admin_jobs_empty_when_none_run(client: TestClient):
    # /admin/jobs reflects the in-process job table; module scope keeps it
    # consistent across this file.
    r = client.get("/admin/jobs")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_admin_unknown_job_returns_404(client: TestClient):
    r = client.get("/admin/jobs/does-not-exist")
    assert r.status_code == 404
    assert r.json()["detail"] == "unknown_job_id"


def test_admin_stats_shape(client: TestClient):
    r = client.get("/admin/stats")
    assert r.status_code == 200
    body = r.json()
    for key in (
        "version",
        "jobs_total",
        "jobs_done",
        "jobs_failed",
        "jobs_cancelled",
        "files_parsed_total",
        "parse_error_total",
        "files_by_language",
        "started_at",
    ):
        assert key in body
    # Single source of truth: stats version matches /admin/version.
    assert body["version"] == client.get("/admin/version").json()["version"]


def test_openapi_publishes_tools(client: TestClient):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    paths = schema["paths"]
    assert "/health" in paths
    assert "/admin/version" in paths
    assert "/admin/jobs" in paths
    # Auto-discoverable by dashboard authors as the plan promises.


# ---------------------------------------------------------------------------
# MCP /sse — tools/list, tools/call


def test_mcp_initialize(client: TestClient):
    body, _ = _mcp(
        client,
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0.0"},
        },
    )
    info = body["result"]["serverInfo"]
    assert info["name"] == "deco-assaying"


def test_mcp_tools_list(client: TestClient):
    sid = _initialize(client)
    body, _ = _mcp(client, "tools/list", req_id=2, session_id=sid)
    tools = body["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {
        "analyze_file",
        "index_repo",
        "get_job_status",
        "cancel_job",
        "get_manifest",
        "get_tree",
        "get_top_level_symbols",
        "get_all_symbols",
        "get_languages",
        "get_errors",
        "get_file_analysis",
        "get_analysis_index",
        "list_job_files",
        "get_log_events",
        "list_supported_languages",
        "detect_language",
    }


def test_mcp_list_supported_languages(client: TestClient):
    sid = _initialize(client)
    payload = _call_tool(client, sid, "list_supported_languages", {})
    fully = {row["id"] for row in payload if row["has_full_support"]}
    assert {"python", "typescript", "go", "rust"} <= fully


def test_mcp_detect_language(client: TestClient):
    sid = _initialize(client)
    payload = _call_tool(client, sid, "detect_language", {"path": "foo.py"})
    assert payload == {"language": "python"}

    payload = _call_tool(
        client,
        sid,
        "detect_language",
        {"path": "noext", "first_line": "#!/usr/bin/env python3"},
        req_id=101,
    )
    assert payload == {"language": "python"}


def test_mcp_analyze_file_python(client: TestClient):
    sid = _initialize(client)
    src = '"""doc."""\n\ndef hello(name): return f"hi {name}"\n'
    payload = _call_tool(
        client,
        sid,
        "analyze_file",
        {"content": src, "filename": "alpha.py"},
        req_id=200,
    )
    assert payload["file"]["language"] == "python"
    assert payload["module_doc"] == "doc."
    qnames = {s["qualified_name"] for s in payload["symbols"]}
    assert "hello" in qnames
    assert payload["parse"]["ok"] is True


def test_mcp_analyze_file_unknown_language_envelope(client: TestClient):
    sid = _initialize(client)
    payload = _call_tool(
        client,
        sid,
        "analyze_file",
        {"content": "hello world", "filename": "thing.xyz"},
        req_id=201,
    )
    assert payload["file"]["language"] == ""
    assert payload["parse"]["ok"] is False
    assert payload["parse"]["reason"] == "no_parser"


# ---------------------------------------------------------------------------
# MCP /sse — index_repo end-to-end


def test_mcp_index_repo_end_to_end(client: TestClient, tmp_path: Path, output_root: Path):
    sid = _initialize(client)
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "alpha.py").write_text('"""alpha."""\n\ndef f(): return 1\n')
    (src / "pkg" / "beta.py").write_text("class B:\n    pass\n")

    started = _call_tool(
        client,
        sid,
        "index_repo",
        {"source": str(src)},
        req_id=300,
    )
    # The MCP-facing index_repo response is intentionally minimal — just
    # job_id. Host-side paths (output_path, manifest_path, log_path) are
    # noise to a remote LLM and stripped on this surface.
    assert started.keys() == {"job_id"}
    job_id = started["job_id"]
    # We can still cross-check the on-disk dir via the admin API.
    out = output_root / job_id

    # Poll get_job_status until done.
    deadline = time.time() + 20
    while time.time() < deadline:
        snap = _call_tool(
            client,
            sid,
            "get_job_status",
            {"job_id": job_id},
            req_id=int(time.time() * 1000) % 1_000_000,
        )
        if snap.get("state") in ("done", "failed", "cancelled"):
            break
        time.sleep(0.05)
    else:
        pytest.fail("index_repo job did not finish in time")

    assert snap["state"] == "done", f"job snap: {snap}"
    # No output_path / manifest_path / log_path on the LLM-facing snapshot.
    assert "output_path" not in snap
    assert "manifest_path" not in snap
    assert "log_path" not in snap
    assert snap["progress"]["files_done"] >= 2
    assert snap["progress"]["files_total"] >= 2

    # The artifacts still exist on disk; verify via the admin API which
    # is allowed to expose host paths.
    admin = client.get(f"/admin/jobs/{job_id}").json()
    manifest_path = Path(admin["manifest_path"])
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert "python" in manifest["languages"]

    # Per-file artifacts mirror the source tree.
    assert (out / "files" / "alpha.py.json").exists()
    assert (out / "files" / "pkg" / "beta.py.json").exists()


def test_mcp_get_job_status_unknown(client: TestClient):
    sid = _initialize(client)
    payload = _call_tool(
        client,
        sid,
        "get_job_status",
        {"job_id": "does-not-exist"},
        req_id=400,
    )
    assert payload == {"error": "unknown_job_id"}


def test_mcp_cancel_unknown_job(client: TestClient):
    sid = _initialize(client)
    payload = _call_tool(
        client,
        sid,
        "cancel_job",
        {"job_id": "does-not-exist"},
        req_id=401,
    )
    assert payload == {"ok": False}


@pytest.mark.network
def test_mcp_index_repo_clones_public_github(client: TestClient, output_root: Path):
    """End-to-end clone of this project's own public GitHub repo.

    Marked `network` so CI environments without internet (or with a flaky
    git host) can deselect via `pytest -m "not network"`. Locally this
    exercises the full happy path: validate URL -> partial clone into
    output_path/.source/ -> walk -> analyze every file -> write rollups.
    """
    sid = _initialize(client)

    started = _call_tool(
        client,
        sid,
        "index_repo",
        {"source": "https://github.com/garycoding/deco-assaying"},
        req_id=600,
    )
    job_id = started["job_id"]
    out = output_root / job_id

    # Clone + analyze every file: generous timeout for slow networks.
    deadline = time.time() + 120
    snap: dict = {}
    while time.time() < deadline:
        snap = _call_tool(
            client,
            sid,
            "get_job_status",
            {"job_id": job_id},
            req_id=int(time.time() * 1000) % 1_000_000,
        )
        if snap.get("state") in ("done", "failed", "cancelled"):
            break
        time.sleep(0.2)
    else:
        pytest.fail(f"github-clone job did not finish: {snap}")

    if snap["state"] == "failed" and "git clone" in (snap.get("error") or ""):
        pytest.skip(f"network/git unavailable: {snap['error']}")
    assert snap["state"] == "done", f"job snap: {snap}"

    # The partial clone landed under output_path/.source/.
    assert (out / ".source").is_dir()
    assert (out / ".source" / ".git").is_dir()

    # Manifest reflects what we know about this repo.
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["file_count"] > 10
    assert "python" in manifest["languages"]

    # tree.json lists every path the walker observed (analyzed + skipped).
    tree = json.loads((out / "tree.json").read_text())
    paths = {e["path"] for e in tree["entries"]}
    assert "pyproject.toml" in paths
    assert "README.md" in paths

    # all_symbols.json picks up our own code.
    symbols = json.loads((out / "all_symbols.json").read_text())
    qnames = {e["qualified_name"] for e in symbols["entries"]}
    assert "analyze_inline" in qnames
