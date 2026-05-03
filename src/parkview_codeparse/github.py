"""GitHub Trees API helper.

Optional pre-flight that asks GitHub for `{path: size}` of every file
in a repo's HEAD tree, in one HTTP call, without touching any blobs.
We use it to fill in real sizes for files we deliberately don't fetch
during a `--filter=blob:limit=N` partial clone — those would otherwise
land in `tree.json` with `size: -1`, which is fine but a bit lossy if
cobgrind wants to reason about how big the unfetched files are.

Failures are silent and non-fatal:

- Rate limited (60/hr unauthenticated, 5000/hr with `GITHUB_TOKEN`).
- `truncated: true` from the API on repos with > 100k tree entries —
  we don't paginate sub-trees in v1, just give up.
- Network errors / timeouts.
- 404 / 401 / private repos without a valid token.

In every failure case the caller falls back to the existing
`git ls-tree` path, which still gives a complete path inventory but
with `size: -1` for unfetched blobs.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

GITHUB_REPO_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?$")
_API_BASE = "https://api.github.com"


def parse_github_url(url: str) -> tuple[str, str] | None:
    """Extract `(owner, repo)` from a GitHub HTTPS URL, or None if not one."""
    m = GITHUB_REPO_URL.match(url)
    if not m:
        return None
    return m.group(1), m.group(2)


def fetch_blob_sizes(
    owner: str,
    repo: str,
    *,
    git_ref: str = "",
    token: str | None = None,
    timeout: float = 10.0,
) -> dict[str, int] | None:
    """Return `{path: size_bytes}` for every blob in HEAD's tree.

    Returns None on any failure (rate limit, network error, truncated
    response, 4xx/5xx). Callers should treat that as "size info isn't
    available; record size=-1 for unfetched files".
    """
    ref = git_ref
    if not ref:
        info = _api_get(f"/repos/{owner}/{repo}", token=token, timeout=timeout)
        if info is None:
            return None
        ref = str(info.get("default_branch") or "main")

    tree = _api_get(
        f"/repos/{owner}/{repo}/git/trees/{ref}?recursive=1",
        token=token,
        timeout=timeout,
    )
    if tree is None:
        return None
    if tree.get("truncated"):
        # >100k entries; would need recursive sub-tree fetches to get all of
        # them. v1 just gives up and lets ls-tree handle the path listing
        # without sizes for the unfetched files.
        return None

    out: dict[str, int] = {}
    for entry in tree.get("tree", []):
        if entry.get("type") == "blob":
            try:
                out[entry["path"]] = int(entry.get("size", 0))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def fetch_default_branch(
    owner: str,
    repo: str,
    *,
    token: str | None = None,
    timeout: float = 10.0,
) -> str | None:
    """Resolve the repo's default branch name (e.g. `main`, `trunk`)."""
    info = _api_get(f"/repos/{owner}/{repo}", token=token, timeout=timeout)
    if info is None:
        return None
    branch = info.get("default_branch")
    return str(branch) if branch else None


def fetch_blob_via_raw(
    owner: str,
    repo: str,
    ref: str,
    rel_path: str,
    *,
    token: str | None = None,
    timeout: float = 60.0,
) -> bytes | None:
    """Fetch a single file's bytes from `raw.githubusercontent.com`.

    `ref` may be a branch name, tag, or commit sha. Returns the raw
    bytes on success or None on any error (network, timeout, 404).

    raw.githubusercontent.com is served from a CDN and is not subject
    to the 60/hr API rate limit; for authenticated requests it accepts
    the same bearer token as the API.
    """
    # GitHub paths come in URL-safe form already, but %-encode anything
    # that would be ambiguous in a URL path component.
    encoded_path = urllib.parse.quote(rel_path, safe="/-._~")
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{encoded_path}"
    headers = {"User-Agent": "parkview-codeparse-server"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def _api_get(
    path: str,
    *,
    token: str | None,
    timeout: float,
) -> dict | None:
    url = f"{_API_BASE}{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
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
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed
