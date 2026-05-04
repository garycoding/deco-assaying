"""Helpers for the `/outputs/{job_id}/...` download API.

Three concerns live here:

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
"""

from __future__ import annotations

import io
import shutil
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

from deco_assaying import config, jobs


class OutputError(Exception):
    """Raised when a /outputs/{id}/... request can't be served safely."""


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
