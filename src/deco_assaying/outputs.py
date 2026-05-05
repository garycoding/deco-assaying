"""Helpers for the `/outputs/{job_id}/...` download API and the
`get_*` artifact-fetch MCP tools.

Concerns:

1. **Path safety.** Every consumer-supplied path is resolved against
   the job's output root and rejected if it escapes (symlink trickery,
   `..`, absolute paths). FastAPI's `{path:path}` route param happily
   accepts traversal sequences, so we own this check.
2. **Directory listing.** A small helper that walks (optionally
   recursively) and returns `{path, size, mtime, is_dir}` rows.
3. **Streaming ZIP.** A generator that pumps a `zipfile.ZipFile` into
   an unseekable buffer, yielding bytes as they accumulate. Lets a
   FastAPI `StreamingResponse` send a multi-GB archive without buffering
   the whole thing in memory.
4. **Artifact reads with optional filtering.** `read_*` helpers parse
   each rollup file and apply LLM-friendly narrowing args (`prefix`,
   `path_prefix`, `kind`) so the model can ask for slices of large
   payloads instead of the whole thing.
"""

from __future__ import annotations

import fnmatch
import io
import json
import shutil
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TypedDict

from deco_assaying import config, jobs


class OutputError(Exception):
    """Raised when a /outputs/{id}/... request can't be served safely."""


class ArtifactMissing(OutputError):
    """A specific artifact (e.g. manifest.json) hasn't been written yet —
    typically because the job is still running or never finished cleanly."""


class LsRow(TypedDict):
    path: str
    size: int
    mtime: float
    is_dir: bool


class JobDirRow(TypedDict):
    job_id: str
    size: int
    mtime: float


def resolve_job_dir(job_id: str) -> Path | None:
    """Look up a job's output dir, preferring the live jobs table.

    The table is bounded (JOB_HISTORY_MAX); after eviction the dir
    can still exist on disk under OUTPUT_ROOT. Falling back to a
    direct filesystem check lets us still serve old artifacts as long
    as the retention sweeper hasn't cleaned them up yet.
    """
    snap = jobs.get_status(job_id)
    if snap is not None:
        path = Path(snap["output_path"])
        if path.is_dir():
            return path
        return None
    candidate = (config.OUTPUT_ROOT / job_id).resolve(strict=False)
    if candidate.is_dir() and _is_under(candidate, config.OUTPUT_ROOT.resolve()):
        return candidate
    return None


def safe_subpath(job_dir: Path, rel: str) -> Path:
    """Resolve `rel` under `job_dir` and reject anything that escapes.

    `rel` may be empty (returns job_dir), a relative path, or a path
    with a leading slash (which we treat as relative). Symlinks are
    followed at resolve() time so a malicious symlink pointing outside
    the job dir gets caught.
    """
    cleaned = rel.lstrip("/").lstrip("\\")
    if not cleaned:
        return job_dir
    target = (job_dir / cleaned).resolve(strict=False)
    if not _is_under(target, job_dir.resolve()):
        raise OutputError(f"path escapes job dir: {rel!r}")
    return target


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def list_dir(job_dir: Path, sub: Path, *, recursive: bool) -> list[LsRow]:
    """Build the listing rows for `GET /outputs/{job_id}/ls`."""
    if not sub.is_dir():
        raise OutputError(f"not a directory: {sub.relative_to(job_dir)}")
    iterator = sub.rglob("*") if recursive else sub.iterdir()
    rows: list[LsRow] = []
    for p in iterator:
        try:
            stat = p.stat()
        except OSError:
            continue
        rows.append(
            LsRow(
                path=str(p.relative_to(job_dir)),
                size=stat.st_size if p.is_file() else 0,
                mtime=stat.st_mtime,
                is_dir=p.is_dir(),
            )
        )
    rows.sort(key=lambda r: r["path"])
    return rows


def is_glob(segment: str) -> bool:
    return any(c in segment for c in "*?[")


def expand_glob(job_dir: Path, pattern: str) -> list[Path]:
    """Expand a path-with-globs against `job_dir`.

    `pattern` is consumer-supplied; we expand via `Path.glob` so `**`
    works for recursion, then post-filter to drop matches that
    somehow escaped the job dir (defense in depth — `Path.glob`
    shouldn't return outside results, but symlinks can).
    """
    matches = sorted(job_dir.glob(pattern))
    job_root = job_dir.resolve()
    return [p for p in matches if p.is_file() and _is_under(p.resolve(), job_root)]


def stream_zip(job_dir: Path, files: list[Path]) -> Iterator[bytes]:
    """Yield a streaming ZIP of `files` (each path arcname'd relative to job_dir).

    Uses an unseekable buffer so `zipfile` writes data descriptors
    inline instead of seeking back to patch local file headers — that
    's what makes streaming actually streaming.
    """
    buf = _ChunkBuffer()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for full in files:
            try:
                arc = str(full.relative_to(job_dir))
            except ValueError:
                continue
            zf.write(full, arcname=arc)
            chunk = buf.take()
            if chunk:
                yield chunk
    final = buf.take()
    if final:
        yield final


class _ChunkBuffer(io.RawIOBase):
    """Unseekable, write-only buffer that hands out accumulated bytes
    via `take()`. Used to drive a streaming `zipfile.ZipFile` from a
    generator."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._offset = 0

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def write(self, b) -> int:  # type: ignore[override]
        view = bytes(b)
        self._buf.extend(view)
        self._offset += len(view)
        return len(view)

    def tell(self) -> int:
        # zipfile calls tell() to know where to write the central directory.
        # Reporting accumulated bytes — even after take() drains the buffer —
        # keeps zipfile's internal offsets correct.
        return self._offset

    def take(self) -> bytes:
        data = bytes(self._buf)
        self._buf.clear()
        return data


def remove_job_dir(job_dir: Path) -> None:
    """Recursively delete the job dir. Best-effort."""
    if job_dir.is_dir():
        shutil.rmtree(job_dir, ignore_errors=False)


def list_outputs_root() -> list[JobDirRow]:
    """Walk OUTPUT_ROOT (one level deep) and report each job dir's
    size + mtime. Used by `GET /admin/outputs` for ops."""
    root = config.OUTPUT_ROOT
    if not root.is_dir():
        return []
    rows: list[JobDirRow] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        rows.append(
            JobDirRow(
                job_id=child.name,
                size=_dir_size(child),
                mtime=stat.st_mtime,
            )
        )
    return rows


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


# ---------------------------------------------------------------------------
# Artifact reads — parsed JSON, with narrowing args for the bigger rollups.
#
# Each helper raises ArtifactMissing if the on-disk file isn't there yet
# (e.g. the job is still running). The MCP tool layer turns that into a
# structured error result for the LLM.


def _load_json(job_dir: Path, name: str) -> Any:
    # `name` is hardcoded by every current caller, but route through
    # `safe_subpath` anyway — costs nothing and means a future caller
    # passing user-supplied input can't sneak a traversal in.
    path = safe_subpath(job_dir, name)
    if not path.is_file():
        raise ArtifactMissing(f"{name} not present (job not done yet?)")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_manifest(job_dir: Path) -> dict[str, Any]:
    """Repo-level rollup with one ergonomics tweak for LLM consumers:
    `languages_by_count` is the same data as the `languages` dict but
    pre-sorted by file count descending — saves the model from sorting
    a JSON object before it can reason about "the dominant language."
    """
    raw = _load_json(job_dir, "manifest.json")
    langs = raw.get("languages") or {}
    raw["languages_by_count"] = [
        {"language": lang, "file_count": count}
        for lang, count in sorted(langs.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return raw


def read_languages(job_dir: Path) -> dict[str, Any]:
    return _load_json(job_dir, "languages.json")


def read_errors(job_dir: Path) -> dict[str, Any]:
    return _load_json(job_dir, "errors.json")


def read_tree(
    job_dir: Path,
    *,
    path_prefix: str = "",
    analyzed_only: bool = False,
) -> dict[str, Any]:
    """`tree.json` with optional path-prefix and analyzed-only filters.

    Comparison is case-sensitive forward-slash prefix match.

    Includes byte totals (returned + unfiltered repo total) so an LLM
    can see, before drilling further, whether a subdirectory is "8 KB
    of one Python module" or "200 MB of vendored fixtures."
    """
    raw = _load_json(job_dir, "tree.json")
    all_entries = raw.get("entries", [])
    entries = all_entries
    if path_prefix:
        normalized = path_prefix.lstrip("/")
        entries = [e for e in entries if e["path"].startswith(normalized)]
    if analyzed_only:
        entries = [e for e in entries if e.get("analyzed")]
    return {
        "entries": entries,
        "filters": {"path_prefix": path_prefix, "analyzed_only": analyzed_only},
        "total_in_repo": len(all_entries),
        "total_returned": len(entries),
        "total_size_bytes": sum(e.get("size", 0) for e in entries),
        "total_size_bytes_in_repo": sum(e.get("size", 0) for e in all_entries),
    }


def _read_filtered_symbols(
    job_dir: Path,
    artifact_name: str,
    *,
    prefix: str,
    kind: str,
    file_prefix: str,
) -> dict[str, Any]:
    """Shared body for `read_all_symbols` and `read_top_level_symbols`.
    Same filter logic, different on-disk file."""
    raw = _load_json(job_dir, artifact_name)
    entries = raw.get("entries", [])
    if prefix:
        entries = [e for e in entries if e["qualified_name"].startswith(prefix)]
    if kind:
        entries = [e for e in entries if e.get("kind") == kind]
    if file_prefix:
        normalized = file_prefix.lstrip("/")
        entries = [e for e in entries if e["file"].startswith(normalized)]
    return {
        "entries": entries,
        "filters": {"prefix": prefix, "kind": kind, "file_prefix": file_prefix},
        "total_in_repo": len(raw.get("entries", [])),
        "total_returned": len(entries),
    }


def read_all_symbols(
    job_dir: Path,
    *,
    prefix: str = "",
    kind: str = "",
    file_prefix: str = "",
) -> dict[str, Any]:
    """`all_symbols.json` — every definition across every analyzed
    file (including methods, nested classes, and synthetic module
    rollups). Filterable by qualified-name prefix, kind, and/or
    file-path prefix; filters AND-combine."""
    return _read_filtered_symbols(
        job_dir,
        "all_symbols.json",
        prefix=prefix,
        kind=kind,
        file_prefix=file_prefix,
    )


def read_top_level_symbols(
    job_dir: Path,
    *,
    prefix: str = "",
    kind: str = "",
    file_prefix: str = "",
) -> dict[str, Any]:
    """`top_level_symbols.json` — the cheap view: only module-level
    definitions (no dot in qualified_name, kind != "module"). Same
    filter args and response shape as `read_all_symbols`."""
    return _read_filtered_symbols(
        job_dir,
        "top_level_symbols.json",
        prefix=prefix,
        kind=kind,
        file_prefix=file_prefix,
    )


def read_analysis_index(job_dir: Path) -> dict[str, Any]:
    """`analysis_index.json` — sizes + URLs for every artifact this
    job produced. Read after `manifest.json` to plan which artifacts
    to fetch (especially on large repos where some payloads exceed
    the LLM's context window)."""
    return _load_json(job_dir, "analysis_index.json")


_FILE_ANALYSIS_SECTIONS = (
    "file",
    "module_doc",
    "symbols",
    "imports",
    "exports",
    "references",
    "literals_of_interest",
    "chunks",
    "metrics",
    "parse",
)


def read_file_analysis(
    job_dir: Path,
    rel_path: str,
    *,
    sections: list[str] | None = None,
) -> dict[str, Any]:
    """One per-file artifact under `files/`, optionally trimmed to a
    subset of top-level sections.

    `rel_path` is the source path (e.g. `src/foo.py`); we append `.json`
    and resolve under `files/`. Path-traversal-safe.
    """
    files_dir = safe_subpath(job_dir, "files")
    artifact = safe_subpath(job_dir, f"files/{rel_path.lstrip('/')}.json")
    if not _is_under(artifact.resolve(), files_dir.resolve()):
        raise OutputError(f"path escapes files/: {rel_path!r}")
    if not artifact.is_file():
        raise ArtifactMissing(f"no analysis for {rel_path!r}")
    with open(artifact, encoding="utf-8") as f:
        data = json.load(f)
    if sections is None:
        return data
    invalid = set(sections) - set(_FILE_ANALYSIS_SECTIONS)
    if invalid:
        raise OutputError(f"unknown sections: {sorted(invalid)}; valid: {list(_FILE_ANALYSIS_SECTIONS)}")
    return {k: data.get(k) for k in sections if k in data}


def list_file_artifacts(job_dir: Path, *, glob: str = "") -> dict[str, Any]:
    """List every per-file artifact path under `files/` (without the
    `.json` suffix, so the caller sees source-relative paths).

    Optional `glob` is fnmatch-style and matches against the source
    path (e.g. `src/**/*.py` — but recursion `**` works only via
    Path.glob, so we use that). Empty glob → list everything.
    """
    files_dir = job_dir / "files"
    if not files_dir.is_dir():
        raise ArtifactMissing("files/ not present (job not done yet?)")
    paths: list[str] = []
    for artifact in files_dir.rglob("*.json"):
        if not artifact.is_file():
            continue
        rel = artifact.relative_to(files_dir).as_posix()
        # Strip the `.json` extension so the caller sees the original source path.
        if rel.endswith(".json"):
            rel = rel[: -len(".json")]
        paths.append(rel)
    paths.sort()
    if glob:
        paths = [p for p in paths if fnmatch.fnmatch(p, glob)]
    return {"paths": paths, "count": len(paths), "glob": glob}
