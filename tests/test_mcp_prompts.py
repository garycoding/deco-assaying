"""Tests for the MCP `prompts/list` + `prompts/get` surface.

The deco-assaying server ships two workflow prompts so any client
picking it up inherits the recommended workflow without reading the
README. These tests drive both prompts over the wire protocol.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(mcp_client: TestClient) -> TestClient:
    return mcp_client


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
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text!r}"
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
    )
    assert body["result"]["serverInfo"]["name"] == "deco-assaying"
    return headers.get("mcp-session-id", "")


# ---------------------------------------------------------------------------
# prompts/list


def test_prompts_list(client):
    sid = _initialize(client)
    body, _ = _mcp(client, "prompts/list", req_id=2, session_id=sid)
    prompts = body["result"]["prompts"]
    names = {p["name"] for p in prompts}
    assert names == {"analyze_repo", "explore_finished_job"}
    # Each advertises its arguments correctly.
    by_name = {p["name"]: p for p in prompts}
    analyze_args = {a["name"]: a for a in by_name["analyze_repo"]["arguments"]}
    assert analyze_args["source"]["required"] is True
    assert analyze_args["focus"]["required"] is False
    explore_args = {a["name"]: a for a in by_name["explore_finished_job"]["arguments"]}
    assert explore_args["job_id"]["required"] is True
    assert explore_args["question"]["required"] is True


# ---------------------------------------------------------------------------
# prompts/get — analyze_repo


def test_prompts_get_analyze_repo_minimal(client):
    sid = _initialize(client)
    body, _ = _mcp(
        client,
        "prompts/get",
        {"name": "analyze_repo", "arguments": {"source": "https://github.com/foo/bar"}},
        req_id=3,
        session_id=sid,
    )
    msg = body["result"]["messages"][0]
    text = msg["content"]["text"]
    # Source is interpolated.
    assert "https://github.com/foo/bar" in text
    # Workflow steps are present.
    assert "index_repo" in text
    assert "get_job_status" in text
    assert "get_manifest" in text
    assert "get_analysis_index" in text
    assert "get_top_level_symbols" in text
    # The URL-fallback paragraph is the whole point — it must be there.
    assert "url" in text.lower()
    assert "context window" in text.lower()


def test_prompts_get_analyze_repo_with_focus(client):
    sid = _initialize(client)
    body, _ = _mcp(
        client,
        "prompts/get",
        {
            "name": "analyze_repo",
            "arguments": {"source": "/local/path", "focus": "the auth module"},
        },
        req_id=4,
        session_id=sid,
    )
    text = body["result"]["messages"][0]["content"]["text"]
    assert "the auth module" in text


# ---------------------------------------------------------------------------
# prompts/get — explore_finished_job


def test_prompts_get_explore_finished_job(client):
    sid = _initialize(client)
    body, _ = _mcp(
        client,
        "prompts/get",
        {
            "name": "explore_finished_job",
            "arguments": {
                "job_id": "abc1234567890def",
                "question": "what does this repo do?",
            },
        },
        req_id=5,
        session_id=sid,
    )
    text = body["result"]["messages"][0]["content"]["text"]
    assert "abc1234567890def" in text
    assert "what does this repo do?" in text
    # URL-fallback paragraph still present in this prompt.
    assert "url" in text.lower()


# ---------------------------------------------------------------------------
# Unknown prompt name


def test_prompts_get_unknown(client):
    sid = _initialize(client)
    body, _ = _mcp(
        client,
        "prompts/get",
        {"name": "no-such-prompt", "arguments": {}},
        req_id=6,
        session_id=sid,
    )
    # The MCP server returns a JSON-RPC error for unknown prompts.
    assert "error" in body or body.get("result") is None


# ---------------------------------------------------------------------------
# Required-argument validation


def test_prompts_get_analyze_repo_missing_source(client):
    """`analyze_repo` requires `source`; calling without it must raise
    rather than silently returning a prompt with empty `{source}`."""
    sid = _initialize(client)
    body, _ = _mcp(
        client,
        "prompts/get",
        {"name": "analyze_repo", "arguments": {}},
        req_id=7,
        session_id=sid,
    )
    assert "error" in body or body.get("result") is None


def test_prompts_get_explore_missing_job_id(client):
    sid = _initialize(client)
    body, _ = _mcp(
        client,
        "prompts/get",
        {"name": "explore_finished_job", "arguments": {"question": "what?"}},
        req_id=8,
        session_id=sid,
    )
    assert "error" in body or body.get("result") is None


def test_prompts_get_explore_missing_question(client):
    sid = _initialize(client)
    body, _ = _mcp(
        client,
        "prompts/get",
        {"name": "explore_finished_job", "arguments": {"job_id": "abc"}},
        req_id=9,
        session_id=sid,
    )
    assert "error" in body or body.get("result") is None
