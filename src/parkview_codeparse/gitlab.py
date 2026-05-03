"""GitLab provider.

Implements the provider protocol for gitlab.com repos. Two GitLab APIs
are involved because their REST tree endpoint deliberately omits blob
size:

1. REST `/api/v4/projects/:id/repository/tree?recursive=true` — gives
   us every path + blob sha, paginated. Cheap; one call per page of 100.

2. GraphQL `repository.blobs(paths: [...]).nodes { path size }` — fills
   in sizes for the paths we just enumerated, batched at 100 paths per
   query. This is the GitLab-specific bit: GitHub's REST tree includes
   `size` directly, GitLab's doesn't.

URL parsing supports nested groups (`gitlab.com/group/subgroup/repo`)
because that's a common GitLab layout. The "owner" returned to callers
may therefore contain `/` characters; functions that hit the GitLab
API URL-encode the full `owner/repo` path; the raw-URL helper passes
slashes through.

Failures are silent and non-fatal: any network error, rate limit, 404,
or unexpected response shape returns None. The caller falls back to
the single-clone path with `size=-1` for unfetched blobs.

Auth: set `GITLAB_TOKEN` to a personal-access token. Used both for
private-repo access and to lift the unauthenticated request budget.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

NAME = "gitlab"
TOKEN_ENV = "GITLAB_TOKEN"

# Match `https://gitlab.com/<one-or-more-slash-separated-segments>` then
# strip an optional `.git` suffix. The full path is split into owner +
# repo by us (last segment = repo, everything before = owner). We keep
# the segment regex strict so we never URL-encode anything malicious.
_GITLAB_URL = re.compile(r"^https://gitlab\.com/(?P<path>[A-Za-z0-9_.\-/]+?)(?:\.git)?/?$")
_SEGMENT = re.compile(r"^[A-Za-z0-9_.\-]+$")

_API_BASE = "https://gitlab.com"
_REST_BASE = f"{_API_BASE}/api/v4"
_GRAPHQL_URL = f"{_API_BASE}/api/graphql"

# REST tree page size is documented to max out at 100. We respect it.
_TREE_PAGE_SIZE = 100
# Generous safety net for very large repos. 100 pages * 100 entries =
# 10 000 files; bigger than that is rare and the GraphQL call would
# also need its own batching anyway.
_MAX_TREE_PAGES = 100
# GitLab's GraphQL `blobs(paths:)` accepts a list; we batch to keep
# individual requests modest.
_BLOB_PATHS_PER_QUERY = 100


def env_token() -> str | None:
    """Read the provider-specific token env var (`GITLAB_TOKEN`)."""
    return os.environ.get(TOKEN_ENV) or None


def parse_url(url: str) -> tuple[str, str] | None:
    """Extract `(owner, repo)` from a GitLab HTTPS URL, or None if not one.

    `owner` may contain `/` for nested groups (`group/subgroup/...`).
    """
    m = _GITLAB_URL.match(url)
    if not m:
        return None
    parts = m.group("path").split("/")
    if len(parts) < 2:
        return None
    for p in parts:
        if not _SEGMENT.match(p) or p in (".", ".."):
            return None
    owner = "/".join(parts[:-1])
    repo = parts[-1]
    return owner, repo


def fetch_default_branch(
    owner: str,
    repo: str,
    *,
    token: str | None = None,
    timeout: float = 10.0,
) -> str | None:
    """Resolve the repo's default branch via REST `GET /projects/:id`."""
    pid = _project_id(owner, repo)
    info = _api_get(f"{_REST_BASE}/projects/{pid}", token=token, timeout=timeout)
    if info is None or not isinstance(info, dict):
        return None
    branch = info.get("default_branch")
    return str(branch) if branch else None


def fetch_blob_sizes(
    owner: str,
    repo: str,
    *,
    git_ref: str = "",
    token: str | None = None,
    timeout: float = 30.0,
) -> dict[str, int] | None:
    """Return `{path: size_bytes}` for every blob in HEAD's tree.

    Requires two API calls: REST tree to enumerate paths, then GraphQL
    to look up sizes (in batches of 100). Returns None on any failure.
    """
    ref = git_ref or fetch_default_branch(owner, repo, token=token, timeout=timeout)
    if not ref:
        return None

    paths = _collect_tree_paths(owner, repo, ref, token=token, timeout=timeout)
    if paths is None:
        return None
    if not paths:
        return {}

    sizes: dict[str, int] = {}
    full_path = f"{owner}/{repo}"
    for start in range(0, len(paths), _BLOB_PATHS_PER_QUERY):
        batch = paths[start : start + _BLOB_PATHS_PER_QUERY]
        chunk = _graphql_blob_sizes(full_path, ref, batch, token=token, timeout=timeout)
        if chunk is None:
            return None
        sizes.update(chunk)
    return sizes


def fetch_blob_via_raw(
    owner: str,
    repo: str,
    ref: str,
    rel_path: str,
    *,
    token: str | None = None,
    timeout: float = 60.0,
) -> bytes | None:
    """Fetch one file via `gitlab.com/{owner}/{repo}/-/raw/{ref}/{path}`.

    `owner` may contain unencoded slashes for nested groups. `ref` is
    URL-encoded to be safe for branch names with `/`. The path is
    encoded conservatively (slashes preserved as separators).
    """
    encoded_owner = "/".join(urllib.parse.quote(p, safe="") for p in owner.split("/"))
    encoded_repo = urllib.parse.quote(repo, safe="")
    encoded_ref = urllib.parse.quote(ref, safe="/-._~")
    encoded_path = urllib.parse.quote(rel_path, safe="/-._~")
    url = f"{_API_BASE}/{encoded_owner}/{encoded_repo}/-/raw/{encoded_ref}/{encoded_path}"
    headers = {"User-Agent": "parkview-codeparse-server"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


# ---------------------------------------------------------------------------
# Internals


def _project_id(owner: str, repo: str) -> str:
    """URL-encoded `owner/repo` — the format GitLab REST uses as a project id."""
    return urllib.parse.quote(f"{owner}/{repo}", safe="")


def _collect_tree_paths(
    owner: str,
    repo: str,
    ref: str,
    *,
    token: str | None,
    timeout: float,
) -> list[str] | None:
    """Walk the REST tree pages and return every blob path in HEAD."""
    pid = _project_id(owner, repo)
    paths: list[str] = []
    encoded_ref = urllib.parse.quote(ref, safe="/-._~")
    for page in range(1, _MAX_TREE_PAGES + 1):
        url = (
            f"{_REST_BASE}/projects/{pid}/repository/tree"
            f"?recursive=true&per_page={_TREE_PAGE_SIZE}&page={page}&ref={encoded_ref}"
        )
        result = _api_get(url, token=token, timeout=timeout)
        if result is None or not isinstance(result, list):
            return None
        for entry in result:
            if isinstance(entry, dict) and entry.get("type") == "blob":
                p = entry.get("path")
                if isinstance(p, str):
                    paths.append(p)
        if len(result) < _TREE_PAGE_SIZE:
            break
    return paths


def _graphql_blob_sizes(
    full_path: str,
    ref: str,
    paths: list[str],
    *,
    token: str | None,
    timeout: float,
) -> dict[str, int] | None:
    """One GraphQL call that returns `{path: size}` for the given paths."""
    query = (
        "query($fullPath: ID!, $paths: [String!]!, $ref: String!) {"
        "  project(fullPath: $fullPath) {"
        "    repository {"
        "      blobs(paths: $paths, ref: $ref) {"
        "        nodes { path size }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    variables = {"fullPath": full_path, "paths": paths, "ref": ref}
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "parkview-codeparse-server",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(_GRAPHQL_URL, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or parsed.get("errors"):
        return None
    project = (parsed.get("data") or {}).get("project") or {}
    repo_obj = (project or {}).get("repository") or {}
    blobs = (repo_obj or {}).get("blobs") or {}
    nodes = blobs.get("nodes") or []
    out: dict[str, int] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        path = node.get("path")
        size = node.get("size")
        if isinstance(path, str) and size is not None:
            try:
                out[path] = int(size)
            except (TypeError, ValueError):
                continue
    return out


def _api_get(
    url: str,
    *,
    token: str | None,
    timeout: float,
) -> Any:
    """GET a JSON-returning REST endpoint. Returns the decoded JSON value
    or None on any failure (network, timeout, 4xx/5xx, decode error).
    Return type is `Any` because GitLab's tree endpoint returns a JSON
    array while project info returns an object — callers narrow with
    isinstance.
    """
    headers = {
        "Accept": "application/json",
        "User-Agent": "parkview-codeparse-server",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None
