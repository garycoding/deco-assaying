"""Extract entities embedded in string literals.

URLs, file paths, env-var names, SQL fragments, and route strings inside
source files are gold for an LLM-Wiki-style consumer: they connect code to
external services, config, and infrastructure. This module pulls them out
of any string literal a tree-sitter analyzer hands us, regardless of
language.

The matchers are deliberately conservative — false positives here propagate
into the wiki as bogus entities — so when in doubt, drop the candidate.
"""

from __future__ import annotations

import re
from typing import Any

# A URL with an explicit scheme. Stops at whitespace and most punctuation
# that wouldn't be inside a URL. Trailing `.,;:!?)]}` is stripped by hand.
_URL = re.compile(r"\b(?:https?|wss?|grpc|postgres|mysql|redis|s3|file)://[^\s'\"<>`]+")

# Filesystem-ish paths. Absolute (`/foo/bar`), home (`~/foo`), or
# `./relative/path`. We require at least two segments and at least one slash
# inside the matched span to keep noise down.
_PATH = re.compile(r"(?:(?<![A-Za-z0-9_/])(?:/|~/|\./|\.\./)[A-Za-z0-9_./\-]+/[A-Za-z0-9_./\-]+)")

# Env-var names: a SCREAMING_SNAKE_CASE token followed by an env-var-y
# context (an `=` sign, a getenv-style call earlier in the line). The string
# itself is just the candidate name; the source-context check happens in
# the caller (we look at the surrounding bytes).
_ENV_NAME = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")

# A SQL statement fragment. We match the common DML/DDL leading keywords;
# anything that follows up to the closing string delimiter is the body.
# Per-language analyzers pass us the *unquoted* string content, so we
# don't have to deal with quote characters here.
_SQL_KEYWORDS = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "CREATE TABLE",
    "CREATE INDEX",
    "ALTER TABLE",
    "DROP TABLE",
    "WITH",
)

# An HTTP route pattern: `/foo`, `/foo/{id}`, `/foo/:id`, `/foo/<id>`.
# Must start with `/` and have at least one path segment.
_ROUTE = re.compile(r"^/[A-Za-z0-9_\-./{}<>:*?]+$")


def extract(value: str, *, span: dict[str, Any], context_before: str = "") -> list[dict[str, Any]]:
    """Pull literals-of-interest from a single string literal.

    `value` is the unquoted string content. `span` is the byte/line span of
    the source string node; we copy it onto each emitted item. `context_before`
    is up to ~64 bytes of source preceding the string, used for the env-var
    contextual check.
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    # Byte ranges within `value` already claimed by a higher-priority match
    # (URLs > paths > everything else). A path that starts inside a URL
    # match is not really a path — it's just the URL's tail.
    claimed: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(s <= start < e or s < end <= e for s, e in claimed)

    def emit(kind: str, val: str, *, start: int = -1, end: int = -1) -> None:
        key = (kind, val)
        if key in seen:
            return
        seen.add(key)
        out.append({"kind": kind, "value": val, "span": span})
        if start >= 0:
            claimed.append((start, end))

    for m in _URL.finditer(value):
        url = m.group(0).rstrip(".,;:!?)]}'\"")
        emit("url", url, start=m.start(), end=m.start() + len(url))

    for m in _PATH.finditer(value):
        if overlaps(m.start(), m.end()):
            continue
        emit("path", m.group(0), start=m.start(), end=m.end())

    stripped = value.strip()
    if _ROUTE.match(stripped) and "/" in stripped[1:]:
        emit("route", stripped)

    upper = value.upper().lstrip()
    for kw in _SQL_KEYWORDS:
        if upper.startswith(kw + " ") or upper.startswith(kw + "\n"):
            emit("sql", " ".join(value.split()))
            break

    if context_before:
        ctx = context_before[-64:].lower()
        if any(needle in ctx for needle in ("getenv", "environ", "env[", "env.get", "process.env")):
            for m in _ENV_NAME.finditer(value):
                emit("env_var", m.group(0))

    return out
