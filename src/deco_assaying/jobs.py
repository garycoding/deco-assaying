"""Indexing-job orchestration.

Public entry point: `start_index_repo(arguments)` returns a job_id and
spawns a background thread that drives the work. The thread:

1. Validates `output_dir` and `source` (security boundary lives in
   `deco_assaying.source`).
2. Resolves the source — for a GitHub URL, shallow-clones into
   `output_dir/.source/`; for a local path, validates and uses it in
   place.
3. Walks the tree (`deco_assaying.walker`), respecting `.gitignore`
   plus our hard-coded skip list and a binary/size sniff.
4. Submits per-file analysis to a `ProcessPoolExecutor`. Each worker
   parses with tree-sitter and runs the language-specific analyzer; the
   result is the per-file JSON shape documented in the plan.
5. As completions arrive, atomically writes
   `output_dir/files/<rel>.json`, appends an event to
   `output_dir/log.jsonl`, and updates the live job entry's counters.
6. On finish: builds the rollups (`manifest.json`, `symbols.json`,
   `languages.json`, `errors.json`) and flips status to `done`.

Cancellation is cooperative: `cancel(job_id)` sets `_cancel=True`. The
orchestrator stops submitting new files between completions; the workers
already in flight finish naturally. The terminal status (`cancelled`) is
written by the orchestrator, never by the cancel-call itself, so a worker
mid-write is never raced.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from deco_assaying import analyze, config, manifest, providers, source, walker
from deco_assaying.config import DEFAULT_MAX_PARTIAL_CLONE_BYTES, JOB_HISTORY_MAX

log = logging.getLogger(__name__)

_lock = threading.Lock()
_jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
_started_at = time.time()

_files_parsed_total = 0
_parse_error_total = 0
_files_by_language: dict[str, int] = {}

_TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})


# ---------------------------------------------------------------------------
# Public entry points (called by routes.py)


def start_index_repo(arguments: dict[str, Any]) -> tuple[str, Path]:
    """Register and start an indexing job.

    The server allocates `config.OUTPUT_ROOT/{job_id}/` for the
    output; callers no longer supply a path. Returns
    `(job_id, output_path)` so the caller knows where to find the
    artifacts (or where the download API will serve them from).

    Raises `source.SourceError` if the output dir can't be created
    (e.g. `OUTPUT_ROOT` is unwritable).
    """
    job_id = uuid.uuid4().hex[:16]
    output_path = source.prepare_output_dir(config.OUTPUT_ROOT, job_id)
    now = time.time()
    job: dict[str, Any] = {
        "job_id": job_id,
        "source": arguments["source"],
        "output_path": str(output_path),
        "git_ref": arguments.get("git_ref") or "",
        "options": _options_from_args(arguments),
        "status": "pending",
        "files_done": 0,
        "files_total": 0,
        "errors_count": 0,
        "started_at": now,
        "finished_at": None,
        "manifest_path": None,
        "log_path": None,
        "error": None,
        "_cancel": False,
    }
    with _lock:
        _jobs[job_id] = job
        _evict_if_full(now_inserting_id=job_id)

    thread = threading.Thread(
        target=_run_job,
        args=(job_id,),
        name=f"index-job-{job_id}",
        daemon=True,
    )
    thread.start()
    return job_id, output_path


def get_status(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        return _public_view(job)


def cancel(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return False
        if job["status"] in _TERMINAL_STATES:
            return True
        job["_cancel"] = True
        return True


def is_active(job_id: str) -> bool:
    """True while the job is in the table and hasn't reached a terminal state.

    Used by the retention sweeper and the DELETE /outputs/{id} endpoint
    to refuse to nuke a still-running job's output dir out from under
    the worker thread.
    """
    with _lock:
        job = _jobs.get(job_id)
        return bool(job and job["status"] not in _TERMINAL_STATES)


def drop(job_id: str) -> bool:
    """Remove a job's table entry. Returns True if it was present.

    The caller is responsible for removing the on-disk output dir; this
    function only touches the in-memory table.
    """
    with _lock:
        if job_id in _jobs:
            del _jobs[job_id]
            return True
        return False


def list_jobs(limit: int = JOB_HISTORY_MAX, status: str | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(limit, JOB_HISTORY_MAX))
    with _lock:
        snapshots = [_public_view(j) for j in reversed(list(_jobs.values()))]
    if status:
        snapshots = [j for j in snapshots if j["state"] == status]
    return [
        {k: v for k, v in s.items() if k not in ("manifest_path", "log_path", "error")}
        for s in snapshots[:limit]
    ]


def read_log(job_id: str, *, from_offset: int = 0, limit: int = 1000) -> dict[str, Any] | None:
    """Tail `log.jsonl` for a job, returning newline-delimited JSON events.

    Reads raw bytes so the returned `next_offset` is a real byte offset into
    the file (decode/re-encode round-tripping would drift on malformed
    UTF-8). A trailing partial line is left unconsumed so the next poll
    picks it up once the writer flushes.
    """
    limit = max(1, min(limit, 100_000))
    with _lock:
        job = _jobs.get(job_id)
    if job is None:
        return None
    log_path = job.get("log_path")
    if not log_path:
        return {"events": [], "next_offset": from_offset}
    try:
        with open(log_path, "rb") as f:
            f.seek(max(0, from_offset))
            data = f.read()
    except FileNotFoundError:
        return {"events": [], "next_offset": from_offset}

    events: list[dict[str, Any]] = []
    consumed = 0
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        consumed += len(line)
        stripped = line.strip()
        if stripped:
            try:
                events.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
        if len(events) >= limit:
            break
    return {"events": events, "next_offset": from_offset + consumed}


def stats() -> dict[str, Any]:
    with _lock:
        all_jobs = list(_jobs.values())
        files_parsed = _files_parsed_total
        parse_errors = _parse_error_total
        by_lang = dict(_files_by_language)
    return {
        "jobs_total": len(all_jobs),
        "jobs_done": sum(1 for j in all_jobs if j["status"] == "done"),
        "jobs_failed": sum(1 for j in all_jobs if j["status"] == "failed"),
        "jobs_cancelled": sum(1 for j in all_jobs if j["status"] == "cancelled"),
        "files_parsed_total": files_parsed,
        "parse_error_total": parse_errors,
        "files_by_language": by_lang,
        "started_at": _started_at,
    }


# ---------------------------------------------------------------------------
# Internals


def _maybe_fetch_provider_sizes(
    src_arg: str,
    git_ref: str,
    options: dict[str, Any],
    log_fh: Any,
) -> dict[str, int] | None:
    """Best-effort provider pre-flight (GitHub Trees API or GitLab
    REST tree + GraphQL).

    Returns `{path: size}` for every blob, or None on any failure (rate
    limit, network error, 4xx, truncated). On None, the caller falls
    back to the single-clone path with `size=-1` for unfetched files.
    """
    if not options.get("provider_api", True):
        return None
    matched = providers.for_url(src_arg)
    if matched is None:
        return None
    provider, owner, repo_name = matched
    token = provider.env_token()
    sizes = provider.fetch_blob_sizes(owner, repo_name, git_ref=git_ref, token=token)
    if sizes is None:
        _emit(log_fh, {"event": "provider_api_unavailable", "provider": provider.NAME})
        return None
    _emit(
        log_fh,
        {
            "event": "provider_api_ok",
            "provider": provider.NAME,
            "n_paths": len(sizes),
            "authenticated": bool(token),
        },
    )
    return sizes


def _run_one_of(
    *,
    job_id: str,
    src_arg: str,
    git_ref: str,
    output_dir: Path,
    files_dir: Path,
    log_fh: Any,
    options: dict[str, Any],
    size_overrides: dict[str, int] | None,
) -> tuple[walker.WalkResult, list[dict[str, Any]]]:
    """Pick a fetch strategy and run it.

    1. Local path → walk on disk, no clone, no network.
    2. Provider URL (GitHub or GitLab) + provider API succeeded +
       planned bytes > threshold → streaming bin-packed mode (no
       clone; download files in batches of <= max_partial_clone_bytes
       via the provider's raw URLs, analyze, delete).
    3. Everything else (including provider-API-failed) → single
       partial clone via `git clone --filter=blob:limit=<max_file_bytes>`.

    Returns the WalkResult (for the manifest's tree.json) and the per-
    file summaries (for the rest of the manifest).
    """
    matched = providers.for_url(src_arg)

    if matched is not None and size_overrides is not None:
        provider, owner, repo_name = matched
        planned = _planned_source_bytes(size_overrides, options)
        cap = options["max_partial_clone_bytes"]
        if planned > cap and not options["eager_clone"]:
            return _run_streaming(
                job_id=job_id,
                provider=provider,
                owner=owner,
                repo_name=repo_name,
                git_ref=git_ref,
                output_dir=output_dir,
                files_dir=files_dir,
                log_fh=log_fh,
                options=options,
                sizes=size_overrides,
            )

    # Single-clone (default for typical-sized repos and for local paths).
    return _run_single_clone(
        job_id=job_id,
        src_arg=src_arg,
        git_ref=git_ref,
        output_dir=output_dir,
        files_dir=files_dir,
        log_fh=log_fh,
        options=options,
        size_overrides=size_overrides,
    )


def _run_single_clone(
    *,
    job_id: str,
    src_arg: str,
    git_ref: str,
    output_dir: Path,
    files_dir: Path,
    log_fh: Any,
    options: dict[str, Any],
    size_overrides: dict[str, int] | None,
) -> tuple[walker.WalkResult, list[dict[str, Any]]]:
    """The original path: clone (or take a local dir), walk on disk, analyze."""
    resolved = source.resolve_source(
        source=src_arg,
        output_dir=output_dir,
        git_ref=git_ref,
        max_blob_bytes=options["max_file_bytes"],
        eager_clone=options["eager_clone"],
    )
    _emit(log_fh, {"event": "source_resolved", "root": str(resolved.root), "mode": "single"})

    walk_result = walker.walk_full(
        resolved.root,
        respect_gitignore=options["respect_gitignore"],
        extra_ignore_globs=options["extra_ignore_globs"],
        max_file_bytes=options["max_file_bytes"],
    )
    walker.annotate_with_unfetched_blobs(
        walk_result,
        resolved.root,
        size_overrides=size_overrides,
    )
    with _lock:
        _jobs[job_id]["files_total"] = len(walk_result.included)
    _emit(
        log_fh,
        {"event": "walk_done", "included": len(walk_result.included), "skipped": len(walk_result.skipped)},
    )
    file_summaries = _process_files(
        job_id=job_id,
        root=resolved.root,
        entries=walk_result.included,
        files_dir=files_dir,
        log_fh=log_fh,
        options=options,
    )
    return walk_result, file_summaries


def _run_streaming(
    *,
    job_id: str,
    provider: Any,
    owner: str,
    repo_name: str,
    git_ref: str,
    output_dir: Path,
    files_dir: Path,
    log_fh: Any,
    options: dict[str, Any],
    sizes: dict[str, int],
) -> tuple[walker.WalkResult, list[dict[str, Any]]]:
    """Streaming mode: no clone. Bin-pack source files into batches of
    <= max_partial_clone_bytes, fetch each batch via the provider's raw
    URLs, analyze, delete the batch, repeat.

    Peak source-side disk = one batch's bytes.
    """
    token = provider.env_token()
    ref = git_ref
    if not ref:
        # Provider API gave us sizes; we still need a ref string for raw URLs.
        ref = provider.fetch_default_branch(owner, repo_name, token=token) or "HEAD"

    scratch = output_dir / ".source"
    if scratch.exists():
        source._safe_clean(scratch)
    scratch.mkdir(parents=True, exist_ok=True)

    # `.gitignore` content (best-effort) so the streaming walker can
    # apply the same filter the local-mode walker does.
    gitignore_text = ""
    if options["respect_gitignore"]:
        gi = provider.fetch_blob_via_raw(owner, repo_name, ref, ".gitignore", token=token)
        if gi is not None:
            gitignore_text = gi.decode("utf-8", errors="replace")

    walk_result = walker.walk_from_inventory(
        sizes=sizes,
        gitignore_text=gitignore_text,
        extra_ignore_globs=options["extra_ignore_globs"],
        max_file_bytes=options["max_file_bytes"],
    )
    with _lock:
        _jobs[job_id]["files_total"] = len(walk_result.included)
    _emit(
        log_fh,
        {
            "event": "source_resolved",
            "root": str(scratch),
            "mode": "streaming",
            "owner": owner,
            "repo": repo_name,
            "ref": ref,
        },
    )
    _emit(
        log_fh,
        {"event": "walk_done", "included": len(walk_result.included), "skipped": len(walk_result.skipped)},
    )

    batches = _bin_pack(walk_result.included, options["max_partial_clone_bytes"])
    _emit(log_fh, {"event": "batches_planned", "batch_count": len(batches)})

    file_summaries = _process_streaming_batches(
        job_id=job_id,
        provider=provider,
        owner=owner,
        repo_name=repo_name,
        ref=ref,
        scratch=scratch,
        batches=batches,
        files_dir=files_dir,
        log_fh=log_fh,
        options=options,
        token=token,
    )
    return walk_result, file_summaries


def _planned_source_bytes(sizes: dict[str, int], options: dict[str, Any]) -> int:
    """Sum of bytes the walker would *want* to analyze (size cap applied,
    no other filter — directory and gitignore filters could reduce this
    further, but planning a switch on a possibly-overestimated number
    is fine; better to switch to streaming when in doubt).
    """
    cap = options["max_file_bytes"]
    return sum(s for s in sizes.values() if 0 < s <= cap)


def _bin_pack(entries: list[walker.TreeEntry], limit: int) -> list[list[walker.TreeEntry]]:
    """Greedy first-fit-decreasing bin packing.

    Each batch's total size is <= `limit` bytes, except for a single
    file that itself exceeds `limit` (which gets its own batch — this
    can't happen for the streaming path under normal options because
    `max_file_bytes` is well below `max_partial_clone_bytes`, but we
    handle it defensively).
    """
    if limit <= 0:
        return [list(entries)]
    sorted_entries = sorted(entries, key=lambda e: -e.size)
    batches: list[list[walker.TreeEntry]] = []
    batch_sizes: list[int] = []
    for entry in sorted_entries:
        placed = False
        for i, current_size in enumerate(batch_sizes):
            if current_size + entry.size <= limit:
                batches[i].append(entry)
                batch_sizes[i] = current_size + entry.size
                placed = True
                break
        if not placed:
            batches.append([entry])
            batch_sizes.append(entry.size)
    return batches


def _process_streaming_batches(
    *,
    job_id: str,
    provider: Any,
    owner: str,
    repo_name: str,
    ref: str,
    scratch: Path,
    batches: list[list[walker.TreeEntry]],
    files_dir: Path,
    log_fh: Any,
    options: dict[str, Any],
    token: str | None,
) -> list[dict[str, Any]]:
    """Sequential batches; within each batch fetch in parallel, analyze
    in parallel, then delete the batch's files before the next."""
    summaries: list[dict[str, Any]] = []
    if not batches:
        return summaries

    # One executor for the whole job; per-batch lifecycle would re-pay
    # the macOS spawn cost on every batch.
    fetch_workers = 16
    with ProcessPoolExecutor() as pool:
        for batch_idx, batch in enumerate(batches):
            if _is_cancelled(job_id):
                break

            batch_bytes = sum(e.size for e in batch)
            _emit(
                log_fh,
                {
                    "event": "batch_start",
                    "index": batch_idx,
                    "files": len(batch),
                    "bytes": batch_bytes,
                },
            )

            fetched = _fetch_batch(
                provider=provider,
                owner=owner,
                repo_name=repo_name,
                ref=ref,
                scratch=scratch,
                batch=batch,
                token=token,
                workers=fetch_workers,
            )

            futures: dict[Any, str] = {}
            for entry in batch:
                if entry.path not in fetched:
                    _emit(
                        log_fh,
                        {"event": "file_failed", "path": entry.path, "error": "raw fetch failed"},
                    )
                    with _lock:
                        _jobs[job_id]["errors_count"] += 1
                        _jobs[job_id]["files_done"] += 1
                    continue
                fut = pool.submit(
                    _worker_disk,
                    entry.path,
                    str(scratch / entry.path),
                    options["include_chunks"],
                    options["chunk_max_tokens"],
                )
                futures[fut] = entry.path

            for fut in as_completed(futures):
                rel = futures[fut]
                _drain_one(fut, rel, files_dir, log_fh, summaries, job_id)

            for entry in batch:
                with contextlib.suppress(OSError):
                    (scratch / entry.path).unlink()
            _emit(log_fh, {"event": "batch_done", "index": batch_idx})
    return summaries


def _fetch_batch(
    *,
    provider: Any,
    owner: str,
    repo_name: str,
    ref: str,
    scratch: Path,
    batch: list[walker.TreeEntry],
    token: str | None,
    workers: int,
) -> set[str]:
    """Fetch each file in `batch` to disk in parallel via the provider's
    raw URL helper.

    Returns the set of paths that landed on disk successfully. Failed
    fetches are silently dropped from the set; the caller decides how
    to log them.
    """
    fetched: set[str] = set()
    fetch_lock = threading.Lock()

    def _one(entry: walker.TreeEntry) -> None:
        content = provider.fetch_blob_via_raw(
            owner,
            repo_name,
            ref,
            entry.path,
            token=token,
        )
        if content is None:
            return
        target = scratch / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_bytes(content)
        except OSError:
            return
        with fetch_lock:
            fetched.add(entry.path)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_one, batch))
    return fetched


def _options_from_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Pluck the per-call options out of the MCP arguments dict.

    `output_dir` and `force` are not options anymore — the server
    always allocates `OUTPUT_ROOT/{job_id}/` and that dir is fresh
    every time, so there's never an existing dir to overwrite.
    """
    max_bytes = int(arguments.get("max_file_bytes", 2 * 1024 * 1024))
    return {
        "respect_gitignore": bool(arguments.get("respect_gitignore", True)),
        "extra_ignore_globs": list(arguments.get("extra_ignore_globs") or []),
        "max_file_bytes": max_bytes,
        "include_chunks": bool(arguments.get("include_chunks", True)),
        "chunk_max_tokens": int(arguments.get("chunk_max_tokens", 800)),
        "eager_clone": bool(arguments.get("eager_clone", False)),
        # `provider_api` is the new name; we accept the old `github_api`
        # for back-compat. Either disables the pre-flight call to the
        # provider's tree/sizes endpoint.
        "provider_api": bool(arguments.get("provider_api", arguments.get("github_api", True))),
        "max_partial_clone_bytes": int(
            arguments.get("max_partial_clone_bytes", DEFAULT_MAX_PARTIAL_CLONE_BYTES)
        ),
    }


def _public_view(job: dict[str, Any]) -> dict[str, Any]:
    """Plan-shape view of a job (state + nested progress)."""
    return {
        "job_id": job["job_id"],
        "source": job["source"],
        "output_path": job["output_path"],
        "state": job["status"],
        "progress": {
            "files_done": job["files_done"],
            "files_total": job["files_total"],
        },
        "errors_count": job["errors_count"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "manifest_path": job["manifest_path"],
        "log_path": job["log_path"],
        "error": job["error"],
    }


def _set_status(job_id: str, status: str, *, error: str | None = None) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["status"] = status
        if status in _TERMINAL_STATES:
            job["finished_at"] = time.time()
        if error is not None:
            job["error"] = error


def _is_cancelled(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        return bool(job and job["_cancel"])


def _evict_if_full(*, now_inserting_id: str) -> None:
    """Drop the oldest *terminal* job; pin active jobs."""
    cap = max(1, JOB_HISTORY_MAX)
    while len(_jobs) > cap:
        for jid, job in _jobs.items():
            if jid != now_inserting_id and job["status"] in _TERMINAL_STATES:
                del _jobs[jid]
                break
        else:
            return


def _run_job(job_id: str) -> None:
    """Background-thread entry point. Owns the executor + log writer."""
    try:
        with _lock:
            job = _jobs[job_id]
            options = job["options"]
            src_arg = job["source"]
            output_dir = Path(job["output_path"])
            git_ref = job["git_ref"]

        # `output_dir` is already created by start_index_repo via
        # source.prepare_output_dir; we just set up the per-job
        # subpaths under it.
        files_dir = output_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        log_path = output_dir / "log.jsonl"

        with _lock:
            _jobs[job_id]["log_path"] = str(log_path)

        with open(log_path, "a", encoding="utf-8") as log_fh:
            _set_status(job_id, "running")

            # Optional pre-flight: ask GitHub's Trees API for path+size
            # of every blob in HEAD (one HTTP call, no blobs fetched).
            # Used both as input to the streaming-vs-single decision and
            # to fill in real sizes for unfetched files in tree.json.
            size_overrides = _maybe_fetch_provider_sizes(src_arg, git_ref, options, log_fh)

            walk_result, file_summaries = _run_one_of(
                job_id=job_id,
                src_arg=src_arg,
                git_ref=git_ref,
                output_dir=output_dir,
                files_dir=files_dir,
                log_fh=log_fh,
                options=options,
                size_overrides=size_overrides,
            )

            elapsed = time.time() - _jobs[job_id]["started_at"]

            if _is_cancelled(job_id):
                _emit(log_fh, {"event": "cancelled"})
                _set_status(job_id, "cancelled")
                return

            with _lock:
                job_snapshot = dict(_jobs[job_id])
            job_snapshot["finished_at"] = time.time()
            manifest.write(
                output_dir=output_dir,
                job=job_snapshot,
                file_summaries=file_summaries,
                walk_result=walk_result,
                elapsed_seconds=elapsed,
            )
            with _lock:
                _jobs[job_id]["manifest_path"] = str(output_dir / "manifest.json")

            _emit(log_fh, {"event": "manifest_written", "elapsed_seconds": elapsed})
            _set_status(job_id, "done")

    except source.SourceError as e:
        log.warning("job %s rejected: %s", job_id, e)
        _set_status(job_id, "failed", error=str(e))
    except Exception as e:
        log.exception("job %s failed: %s", job_id, e)
        _set_status(job_id, "failed", error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}")


def _process_files(
    *,
    job_id: str,
    root: Path,
    entries: list[walker.TreeEntry],
    files_dir: Path,
    log_fh: Any,
    options: dict[str, Any],
) -> list[dict[str, Any]]:
    """Local / eager-clone path: workers read files from disk."""
    summaries: list[dict[str, Any]] = []
    if not entries:
        return summaries

    with ProcessPoolExecutor() as pool:
        futures: dict[Any, str] = {}
        for entry in entries:
            if _is_cancelled(job_id):
                break
            fut = pool.submit(
                _worker_disk,
                entry.path,
                str(root / entry.path),
                options["include_chunks"],
                options["chunk_max_tokens"],
            )
            futures[fut] = entry.path

        for fut in as_completed(futures):
            rel = futures[fut]
            _drain_one(fut, rel, files_dir, log_fh, summaries, job_id)
    return summaries


_LAZY_BATCH_SIZE = 32


def _process_files_lazy(
    *,
    job_id: str,
    clone_dir: Path,
    entries: list[walker.TreeEntry],
    files_dir: Path,
    log_fh: Any,
    options: dict[str, Any],
) -> list[dict[str, Any]]:
    """Partial-clone path: batched `git checkout` + parallel analysis.

    The naive cat-file approach (one blob at a time) issues one network
    round-trip per missing blob in partial-clone mode — for a repo with
    100 wanted source files, that's 100 sequential fetches and dominates
    wall time. This path instead processes entries in batches of
    `_LAZY_BATCH_SIZE`:

      1. `git checkout HEAD -- p1 p2 ... pN` materializes the batch's
         files in one command. git bundles every missing blob in the
         batch into a single fetch over the partial-clone protocol —
         one round-trip instead of N.
      2. The batch's files are analyzed in parallel via
         `ProcessPoolExecutor` (workers read from disk).
      3. Once the batch is done, the files are deleted from the working
         tree before we move on, bounding peak disk to one batch.

    Peak disk on top of `.git/` is therefore ~`_LAZY_BATCH_SIZE` times
    the average wanted-source-file size — typically well under 1 MB. We
    never materialize unwanted files (binaries, vendored deps, etc.) at
    any point; their blobs are never fetched.
    """
    summaries: list[dict[str, Any]] = []
    if not entries:
        return summaries

    # One pool, one git-checkout subprocess per batch. Spawning a new
    # ProcessPoolExecutor per batch was costing ~1s on macOS (spawn, not
    # fork), which dominated wall time on small repos.
    with ProcessPoolExecutor() as pool:
        for start in range(0, len(entries), _LAZY_BATCH_SIZE):
            if _is_cancelled(job_id):
                break
            batch = entries[start : start + _LAZY_BATCH_SIZE]
            _process_lazy_batch(
                job_id=job_id,
                clone_dir=clone_dir,
                batch=batch,
                files_dir=files_dir,
                log_fh=log_fh,
                options=options,
                summaries=summaries,
                pool=pool,
            )
    return summaries


def _process_lazy_batch(
    *,
    job_id: str,
    clone_dir: Path,
    batch: list[walker.TreeEntry],
    files_dir: Path,
    log_fh: Any,
    options: dict[str, Any],
    summaries: list[dict[str, Any]],
    pool: ProcessPoolExecutor,
) -> None:
    """Materialize one batch via `git checkout`, analyze, then clean up."""
    paths = [entry.path for entry in batch]
    cmd = ["git", "-C", str(clone_dir), "checkout", "HEAD", "--", *paths]
    proc = subprocess.run(cmd, shell=False, capture_output=True, text=True)
    if proc.returncode != 0:
        # Log every entry as failed and keep going.
        err = (proc.stderr or proc.stdout or "git checkout failed").strip()
        for entry in batch:
            _emit(log_fh, {"event": "file_failed", "path": entry.path, "error": err})
            with _lock:
                _jobs[job_id]["errors_count"] += 1
                _jobs[job_id]["files_done"] += 1
        return

    try:
        # Post-checkout size filter. We can't filter on size during
        # `walk_git_tree` (size requires fetching the blob) so we do it
        # here, after git has materialized the batch. Files that exceed
        # max_file_bytes get logged-and-skipped without dispatching to
        # the worker pool; their entry stays in tree.json with
        # skip_reason="oversize".
        max_bytes = options["max_file_bytes"]
        futures: dict[Any, str] = {}
        for entry in batch:
            full = clone_dir / entry.path
            try:
                size = full.stat().st_size
            except OSError:
                continue
            entry.size = size  # remember the real size for the manifest tree
            if size > max_bytes:
                entry.analyzed = False
                entry.skip_reason = "oversize"
                _emit(log_fh, {"event": "file_skipped", "path": entry.path, "reason": "oversize"})
                with _lock:
                    _jobs[job_id]["files_done"] += 1
                continue
            fut = pool.submit(
                _worker_disk,
                entry.path,
                str(full),
                options["include_chunks"],
                options["chunk_max_tokens"],
            )
            futures[fut] = entry.path

        for fut in as_completed(futures):
            rel = futures[fut]
            _drain_one(fut, rel, files_dir, log_fh, summaries, job_id)
    finally:
        # Wipe the batch from the working tree so peak disk stays bounded.
        for entry in batch:
            with contextlib.suppress(OSError):
                (clone_dir / entry.path).unlink()


def _drain_one(
    fut: Any,
    rel: str,
    files_dir: Path,
    log_fh: Any,
    summaries: list[dict[str, Any]],
    job_id: str,
) -> None:
    try:
        rel_path, result = fut.result()
    except Exception as e:
        _emit(log_fh, {"event": "file_failed", "path": rel, "error": str(e)})
        with _lock:
            j = _jobs[job_id]
            j["errors_count"] += 1
            j["files_done"] += 1
        return

    artifact_path = files_dir / (rel_path + ".json")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    _write_artifact(artifact_path, result)
    summaries.append(_summarize_for_manifest(rel_path, result))

    global _files_parsed_total, _parse_error_total
    with _lock:
        j = _jobs[job_id]
        j["files_done"] += 1
        if not result["parse"]["ok"]:
            j["errors_count"] += 1
        _files_parsed_total += 1
        if not result["parse"]["ok"]:
            _parse_error_total += 1
        lang = result["file"]["language"] or "unknown"
        _files_by_language[lang] = _files_by_language.get(lang, 0) + 1
    _emit(
        log_fh,
        {
            "event": "file_done",
            "path": rel_path,
            "language": result["file"]["language"],
            "n_symbols": len(result["symbols"]),
            "parse_ok": result["parse"]["ok"],
            "bytes": result["file"]["bytes"],
        },
    )


def _worker_disk(
    rel_path: str,
    abs_path: str,
    include_chunks: bool,
    chunk_max_tokens: int,
) -> tuple[str, dict[str, Any]]:
    """ProcessPoolExecutor worker: read + analyze one file from disk.

    Used by both the local/eager path and the lazy path — in lazy mode
    the orchestrator has just `git checkout`-ed the file into the working
    tree, so the worker reads exactly the same way as the local case.
    """
    with open(abs_path, "rb") as f:
        content_bytes = f.read()
    text = content_bytes.decode("utf-8", errors="replace")
    result = analyze.analyze_inline(
        content=text,
        filename=rel_path,
        include_chunks=include_chunks,
        chunk_max_tokens=chunk_max_tokens,
    )
    return rel_path, result


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def _summarize_for_manifest(rel: str, result: dict[str, Any]) -> dict[str, Any]:
    f = result["file"]
    return {
        "path": rel,
        "language": f["language"],
        "bytes": f["bytes"],
        "loc": f["loc"],
        "is_test": f["is_test"],
        "is_generated": f["is_generated"],
        "is_config": f["is_config"],
        "has_main_guard": result["metrics"]["has_main_guard"],
        "parse_ok": result["parse"]["ok"],
        "error_nodes": result["parse"]["error_nodes"],
        "missing_nodes": result["parse"]["missing_nodes"],
        "parse_reason": result["parse"].get("reason", ""),
    }


def _emit(fh: Any, event: dict[str, Any]) -> None:
    """Append a single event line to log.jsonl with a wall-clock timestamp."""
    event = dict(event)
    event.setdefault("ts", time.time())
    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    fh.flush()
