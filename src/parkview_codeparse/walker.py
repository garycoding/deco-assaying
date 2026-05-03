"""Repository walker.

Yields the (relative_path, absolute_path) of every file we want the
analyzer to look at, applying a layered set of skips:

- The walker's own bookkeeping: `.git/` (always) and `.source/` (our clone
  cache, never indexed as if it were source).
- Vendored / generated directories that bloat the index without value:
  `node_modules`, `vendor`, `target`, `dist`, `build`, `.venv`,
  `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.ty_cache`,
  `.next`, `.cache`, `.idea`, `.vscode`.
- The repository's own `.gitignore` (and nested `.gitignore` files,
  pathspec-style) when `respect_gitignore=True`.
- Caller-supplied `extra_ignore_globs` (additional gitignore-style patterns).
- File size: anything over `max_file_bytes` is skipped.
- Binary files: a NUL-byte sniff over the first 8 KB.

Returned paths are relative to `root` and use forward slashes regardless
of platform, so the artifact filenames are stable across hosts.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pathspec

DEFAULT_DIR_SKIPS: frozenset[str] = frozenset(
    {
        ".git",
        ".source",
        "node_modules",
        "vendor",
        "target",
        "dist",
        "build",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".ty_cache",
        ".mypy_cache",
        ".next",
        ".nuxt",
        ".cache",
        ".idea",
        ".vscode",
    }
)

_SNIFF_BYTES = 8192

# Extension blacklist used when we have to decide "is this binary?"
# without any content — i.e. when planning a streaming fetch from the
# Trees API output, where reading bytes would defeat the whole point.
# In local / single-clone mode we still NUL-sniff (more accurate); this
# list is the fallback for the streaming path.
BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        "png",
        "jpg",
        "jpeg",
        "gif",
        "ico",
        "bmp",
        "tiff",
        "webp",
        "avif",
        "svg",
        "pdf",
        "ai",
        "psd",
        "sketch",
        "zip",
        "tar",
        "gz",
        "bz2",
        "7z",
        "rar",
        "xz",
        "tgz",
        "tbz",
        "zst",
        "exe",
        "dll",
        "so",
        "dylib",
        "a",
        "lib",
        "o",
        "obj",
        "class",
        "jar",
        "war",
        "wasm",
        "pyc",
        "pyd",
        "whl",
        "egg",
        "woff",
        "woff2",
        "ttf",
        "otf",
        "eot",
        "mp3",
        "mp4",
        "mov",
        "avi",
        "mkv",
        "flac",
        "wav",
        "ogg",
        "webm",
        "m4a",
        "m4v",
        "iso",
        "img",
        "dmg",
    }
)


@dataclass
class TreeEntry:
    """One file the walker observed.

    `analyzed=False` means the file passed the directory pruning step but
    a per-file filter rejected it; `skip_reason` records which one. We
    record these so cobgrind has a complete picture of the repo (every
    path the walker saw, what we did with each).
    """

    path: str  # forward-slash relative path
    size: int  # bytes
    analyzed: bool
    skip_reason: str = ""  # "" if analyzed, otherwise: gitignore | binary | oversize | extra_ignore


@dataclass
class WalkResult:
    """Output of a full walk: the included files (to feed to the analyzer)
    plus every other path the walker observed (for the tree.json rollup).
    """

    included: list[TreeEntry] = field(default_factory=list)
    skipped: list[TreeEntry] = field(default_factory=list)

    def all_entries(self) -> list[TreeEntry]:
        return sorted(
            self.included + self.skipped,
            key=lambda e: e.path,
        )


def walk(
    root: Path,
    *,
    respect_gitignore: bool = True,
    extra_ignore_globs: list[str] | None = None,
    max_file_bytes: int = 2 * 1024 * 1024,
) -> Iterator[tuple[str, Path]]:
    """Iterator yielding `(relative_posix_path, absolute_path)` for files we'll analyze.

    Kept for tests and callers that don't care about the skipped-file
    listing. Most production code should use `walk_full` instead so the
    skipped paths can be recorded in the manifest's tree.json.
    """
    for entry in walk_full(
        root,
        respect_gitignore=respect_gitignore,
        extra_ignore_globs=extra_ignore_globs,
        max_file_bytes=max_file_bytes,
    ).included:
        yield entry.path, root / entry.path


def walk_full(
    root: Path,
    *,
    respect_gitignore: bool = True,
    extra_ignore_globs: list[str] | None = None,
    max_file_bytes: int = 2 * 1024 * 1024,
) -> WalkResult:
    """Walk a local directory, recording both included and skipped files.

    Directory-level skips (`.git`, `node_modules`, etc.) are not recorded
    individually — emitting one entry per file under `node_modules` would
    make tree.json useless. We only record per-file decisions (gitignore,
    extra_ignore, oversize, binary).
    """
    root = root.resolve()
    spec_root = _load_gitignore_spec(root) if respect_gitignore else None
    extra_spec = pathspec.GitIgnoreSpec.from_lines(extra_ignore_globs) if extra_ignore_globs else None
    result = WalkResult()

    for dirpath, dirnames, filenames in os.walk(root):
        cur = Path(dirpath)
        rel_dir = cur.relative_to(root)
        dirnames[:] = [d for d in dirnames if not _skip_dir(rel_dir / d, d, spec_root, extra_spec)]

        for fname in filenames:
            rel = (rel_dir / fname).as_posix() if str(rel_dir) != "." else fname
            full = cur / fname
            try:
                size = full.stat().st_size
            except OSError:
                continue

            reason = _file_skip_reason(rel, full, size, spec_root, extra_spec, max_file_bytes)
            entry = TreeEntry(path=rel, size=size, analyzed=(reason == ""), skip_reason=reason)
            (result.included if entry.analyzed else result.skipped).append(entry)
    return result


def walk_from_inventory(
    sizes: dict[str, int],
    *,
    gitignore_text: str = "",
    extra_ignore_globs: list[str] | None = None,
    max_file_bytes: int = 2 * 1024 * 1024,
) -> WalkResult:
    """Build a `WalkResult` purely from a `{path: size}` inventory.

    Used by the streaming-fetch path — we have the full repo listing
    from the GitHub Trees API, so we can decide which files we'll
    analyze without ever cloning anything. Filters applied:

    - DEFAULT_DIR_SKIPS by path component (vendored / generated dirs).
    - `.gitignore` patterns from `gitignore_text` (caller fetches it
      via raw URL or similar; empty string disables the filter).
    - `extra_ignore_globs` from the index_repo options.
    - Size cap from `max_file_bytes` (sizes come straight from Trees API).
    - Binary detection via `BINARY_EXTENSIONS` since we don't have
      content to NUL-sniff.

    Directory-level skips are silent (not recorded in tree.json).
    Per-file skips become `WalkResult.skipped` entries with their reason.
    """
    spec_root = pathspec.GitIgnoreSpec.from_lines(gitignore_text.splitlines()) if gitignore_text else None
    extra_spec = pathspec.GitIgnoreSpec.from_lines(extra_ignore_globs) if extra_ignore_globs else None
    result = WalkResult()
    for rel, size in sizes.items():
        if any(part in DEFAULT_DIR_SKIPS for part in rel.split("/")[:-1]):
            continue  # directory skip; don't list these in tree.json
        reason = ""
        if spec_root is not None and spec_root.match_file(rel):
            reason = "gitignore"
        elif extra_spec is not None and extra_spec.match_file(rel):
            reason = "extra_ignore"
        elif size > max_file_bytes:
            reason = "oversize"
        else:
            name = rel.rsplit("/", 1)[-1].lower()
            if "." in name and name.rsplit(".", 1)[1] in BINARY_EXTENSIONS:
                reason = "binary"
        entry = TreeEntry(path=rel, size=size, analyzed=(reason == ""), skip_reason=reason)
        (result.included if entry.analyzed else result.skipped).append(entry)
    return result


def annotate_with_unfetched_blobs(
    result: WalkResult,
    root: Path,
    *,
    size_overrides: dict[str, int] | None = None,
) -> WalkResult:
    """If `root` is a git working tree (regular or partial clone), add an
    entry to `result.skipped` for every path in HEAD that didn't make it
    onto disk. In a `--filter=blob:limit=N` clone these are the blobs
    that exceeded N bytes — they're real files in the repo we just
    didn't fetch, and cobgrind wants them in tree.json so the directory
    map is complete.

    Cheap: `git ls-tree -r HEAD` (without `--long`) doesn't read blob
    contents, so the partial-clone protocol isn't triggered. ~10ms for
    repos in the hundreds of files.

    If `size_overrides` is provided (e.g. from the GitHub Trees API
    pre-flight in `parkview_codeparse.github`), unfetched entries get
    real byte sizes instead of `-1`. The overrides only apply to
    entries we add here — already-fetched files keep the size we
    measured from disk.
    """
    git_dir = root / ".git"
    if not git_dir.is_dir():
        return result
    seen: set[str] = {e.path for e in result.included}
    seen.update(e.path for e in result.skipped)
    for rel in _list_head_paths(root):
        if rel in seen:
            continue
        size = -1
        if size_overrides is not None and rel in size_overrides:
            size = size_overrides[rel]
        # Path lives in HEAD but not on disk → unfetched (size > filter cap).
        result.skipped.append(TreeEntry(path=rel, size=size, analyzed=False, skip_reason="oversize"))
    return result


def _list_head_paths(root: Path) -> Iterator[str]:
    """Yield every blob path in HEAD via `git ls-tree -r HEAD` (no --long)."""
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "HEAD"],
        shell=False,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return
    for line in proc.stdout.splitlines():
        # mode SP type SP sha TAB path
        meta, _, path = line.partition("\t")
        if not path:
            continue
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            yield path


def _load_gitignore_spec(root: Path) -> pathspec.GitIgnoreSpec | None:
    """Load `.gitignore` from the repo root.

    We deliberately only honor the *root* `.gitignore` here. Nested
    `.gitignore` files are rare in repos worth indexing and supporting them
    properly means walking with per-directory specs — out of scope for v1.
    """
    gi = root / ".gitignore"
    if not gi.exists():
        return None
    try:
        with open(gi, encoding="utf-8", errors="replace") as f:
            return pathspec.GitIgnoreSpec.from_lines(f)
    except OSError:
        return None


def _skip_dir(
    rel_path: Path,
    name: str,
    spec_root: pathspec.GitIgnoreSpec | None,
    extra_spec: pathspec.GitIgnoreSpec | None,
) -> bool:
    if name in DEFAULT_DIR_SKIPS:
        return True
    rel = rel_path.as_posix() + "/"
    if spec_root is not None and spec_root.match_file(rel):
        return True
    return bool(extra_spec is not None and extra_spec.match_file(rel))


def _file_skip_reason(
    rel: str,
    full: Path,
    size: int,
    spec_root: pathspec.GitIgnoreSpec | None,
    extra_spec: pathspec.GitIgnoreSpec | None,
    max_file_bytes: int,
) -> str:
    if spec_root is not None and spec_root.match_file(rel):
        return "gitignore"
    if extra_spec is not None and extra_spec.match_file(rel):
        return "extra_ignore"
    if size > max_file_bytes:
        return "oversize"
    if _looks_binary(full):
        return "binary"
    return ""


def _looks_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(_SNIFF_BYTES)
    except OSError:
        return True
    return b"\x00" in head


def summarize_skips(root: Path) -> dict[str, Any]:
    """Diagnostic helper used by tests/admin tooling.

    Counts how many entries in `root` would be skipped by name and how
    many by .gitignore, without materializing the full file list.
    """
    spec_root = _load_gitignore_spec(root)
    by_dir_name = 0
    by_gitignore = 0
    for dirpath, dirnames, filenames in os.walk(root):
        cur = Path(dirpath)
        rel_dir = cur.relative_to(root)
        for d in dirnames:
            if d in DEFAULT_DIR_SKIPS:
                by_dir_name += 1
            elif spec_root is not None and spec_root.match_file((rel_dir / d).as_posix() + "/"):
                by_gitignore += 1
        for f in filenames:
            rel = (rel_dir / f).as_posix() if str(rel_dir) != "." else f
            if spec_root is not None and spec_root.match_file(rel):
                by_gitignore += 1
    return {"skipped_by_name": by_dir_name, "skipped_by_gitignore": by_gitignore}
