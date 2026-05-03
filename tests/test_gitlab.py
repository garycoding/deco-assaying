"""Unit tests for the GitLab provider.

Network calls are mocked at the urllib level. We never hit gitlab.com.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

from parkview_codeparse import gitlab, jobs
from parkview_codeparse import gitlab as gitlab_provider


def _mock_response(payload):
    body = json.dumps(payload).encode()

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return body

    return _Ctx()


def _raw_response(body: bytes):
    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return body

    return _Ctx()


# ---------------------------------------------------------------------------
# parse_url


def test_parse_url_simple():
    assert gitlab.parse_url("https://gitlab.com/foo/bar") == ("foo", "bar")


def test_parse_url_strips_dotgit():
    assert gitlab.parse_url("https://gitlab.com/foo/bar.git") == ("foo", "bar")


def test_parse_url_nested_groups():
    assert gitlab.parse_url("https://gitlab.com/group/sub/repo") == ("group/sub", "repo")
    assert gitlab.parse_url("https://gitlab.com/a/b/c/d") == ("a/b/c", "d")


def test_parse_url_rejects_other_hosts():
    assert gitlab.parse_url("https://github.com/foo/bar") is None
    assert gitlab.parse_url("https://gitlab.example.com/foo/bar") is None
    assert gitlab.parse_url("git@gitlab.com:foo/bar.git") is None


def test_parse_url_rejects_missing_repo():
    assert gitlab.parse_url("https://gitlab.com/foo") is None


def test_parse_url_rejects_unsafe_segments():
    assert gitlab.parse_url("https://gitlab.com/foo/../bar") is None


# ---------------------------------------------------------------------------
# fetch_default_branch + fetch_blob_sizes


def test_fetch_default_branch_resolves():
    with patch("urllib.request.urlopen", return_value=_mock_response({"default_branch": "main"})):
        assert gitlab.fetch_default_branch("foo", "bar") == "main"


def test_fetch_default_branch_returns_none_on_error():
    import urllib.error

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("nope")):
        assert gitlab.fetch_default_branch("foo", "bar") is None


def test_fetch_blob_sizes_two_phase():
    """REST tree -> GraphQL sizes; verify both calls happen and the
    `{path: size}` dict is assembled correctly."""
    project_response = {"default_branch": "main"}
    tree_page_1 = [
        {"path": "a.py", "type": "blob"},
        {"path": "b.py", "type": "blob"},
        {"path": "src", "type": "tree"},  # ignored
    ]
    # Page returns < 100 entries, so the walker stops paginating after 1 call.
    graphql_response = {
        "data": {
            "project": {
                "repository": {
                    "blobs": {
                        "nodes": [
                            {"path": "a.py", "size": 100},
                            {"path": "b.py", "size": 200},
                        ]
                    }
                }
            }
        }
    }
    responses = [
        _mock_response(project_response),  # default branch lookup
        _mock_response(tree_page_1),  # tree page 1
        _mock_response(graphql_response),  # graphql sizes
    ]
    with patch("urllib.request.urlopen", side_effect=responses):
        sizes = gitlab.fetch_blob_sizes("foo", "bar")
    assert sizes == {"a.py": 100, "b.py": 200}


def test_fetch_blob_sizes_short_circuits_on_tree_failure():
    """If REST tree returns non-list, the whole call returns None."""
    responses = [
        _mock_response({"default_branch": "main"}),
        _mock_response({"error": "denied"}),  # tree returns dict, not list
    ]
    with patch("urllib.request.urlopen", side_effect=responses):
        assert gitlab.fetch_blob_sizes("foo", "bar") is None


def test_fetch_blob_sizes_returns_none_when_default_branch_missing():
    with patch("urllib.request.urlopen", return_value=_mock_response({})):
        assert gitlab.fetch_blob_sizes("foo", "bar") is None


# ---------------------------------------------------------------------------
# fetch_blob_via_raw


def test_fetch_blob_via_raw_url_shape():
    """Verify the raw URL is built correctly for nested groups + auth header."""
    seen = {}

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.headers)
        return _raw_response(b"contents-here")

    with patch("urllib.request.urlopen", side_effect=fake):
        body = gitlab.fetch_blob_via_raw(
            "group/sub",
            "repo",
            "main",
            "src/a.py",
            token="glpat-test",
        )
    assert body == b"contents-here"
    assert seen["url"] == "https://gitlab.com/group/sub/repo/-/raw/main/src/a.py"
    # urllib title-cases header keys.
    assert seen["headers"].get("Authorization") == "Bearer glpat-test"


# ---------------------------------------------------------------------------
# providers dispatcher recognises gitlab URLs


def test_providers_dispatcher_routes_to_gitlab():
    from parkview_codeparse import providers

    matched = providers.for_url("https://gitlab.com/foo/bar")
    assert matched is not None
    provider, owner, repo = matched
    assert provider.NAME == "gitlab"
    assert (owner, repo) == ("foo", "bar")


def test_providers_dispatcher_routes_to_github():
    from parkview_codeparse import providers

    matched = providers.for_url("https://github.com/foo/bar")
    assert matched is not None
    provider, _owner, _repo = matched
    assert provider.NAME == "github"


# ---------------------------------------------------------------------------
# End-to-end streaming with a mocked GitLab provider


_FAKE_FILES = {
    "alpha.py": '"""alpha."""\n\ndef hello(): return 1\n',
    "pkg/beta.py": "class Beta:\n    def m(self): return 2\n",
    "README.md": "# Demo\n",
}


def _fake_blob_sizes(*args, **kwargs):
    return {p: len(c.encode()) for p, c in _FAKE_FILES.items()}


def _fake_default_branch(*args, **kwargs):
    return "main"


def _fake_blob_via_raw(owner, repo, ref, rel_path, *, token=None, timeout=60.0):
    if rel_path == ".gitignore":
        return None
    if rel_path in _FAKE_FILES:
        return _FAKE_FILES[rel_path].encode()
    return None


def _wait_done(job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = jobs.get_status(job_id)
        assert snap is not None
        if snap["state"] in ("done", "failed", "cancelled"):
            return snap
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_gitlab_streaming_end_to_end(tmp_path: Path):
    """Drive the full job through the GitLab provider with mocked HTTP.

    Forces streaming via a tiny `max_partial_clone_bytes`. Verifies the
    job completes with provider=gitlab in the log events and the
    expected manifest + tree.json shape.
    """
    out = tmp_path / "out"

    with (
        patch.object(gitlab_provider, "fetch_blob_sizes", side_effect=_fake_blob_sizes),
        patch.object(gitlab_provider, "fetch_default_branch", side_effect=_fake_default_branch),
        patch.object(gitlab_provider, "fetch_blob_via_raw", side_effect=_fake_blob_via_raw),
    ):
        job_id = jobs.start_index_repo(
            {
                "source": "https://gitlab.com/fake/repo",
                "output_dir": str(out),
                "max_partial_clone_bytes": 50,  # force streaming
            }
        )
        snap = _wait_done(job_id)

    assert snap["state"] == "done", f"snap: {snap}"
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["file_count"] >= 2
    assert "python" in manifest["languages"]

    log_events = [json.loads(ln) for ln in (out / "log.jsonl").read_text().splitlines() if ln.strip()]
    api_ok = next(e for e in log_events if e["event"] == "provider_api_ok")
    assert api_ok["provider"] == "gitlab"

    src_event = next(e for e in log_events if e["event"] == "source_resolved")
    assert src_event["mode"] == "streaming"

    # Streaming materialized files briefly under .source/ then deleted them.
    leftover = [p for p in (out / ".source").rglob("*") if p.is_file()]
    assert leftover == []
