"""Repo-level rollups: manifest.json, symbols.json, languages.json,
errors.json, tree.json.

These are written when an indexing job finishes. A consumer plans its
ingestion order from the manifest without reading every per-file artifact;
the separate `tree.json` gives it the full repo organization (including
files we deliberately skipped) so it can build a faithful directory map
of the project even for paths it won't summarize.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid runtime import cycle
    from deco_assaying.walker import WalkResult


def write(
    *,
    output_dir: Path,
    job: dict[str, Any],
    file_summaries: list[dict[str, Any]],
    walk_result: WalkResult,
    elapsed_seconds: float,
) -> None:
    """Write all five rollup files atomically."""
    manifest = _build_manifest(
        job=job,
        file_summaries=file_summaries,
        walk_result=walk_result,
        elapsed_seconds=elapsed_seconds,
    )
    languages = _build_languages(file_summaries)
    symbols = _build_symbols_index(output_dir, file_summaries)
    errors = _build_errors(file_summaries)
    tree = _build_tree(walk_result)

    _write_atomic(output_dir / "manifest.json", manifest)
    _write_atomic(output_dir / "languages.json", languages)
    _write_atomic(output_dir / "symbols.json", symbols)
    _write_atomic(output_dir / "errors.json", errors)
    _write_atomic(output_dir / "tree.json", tree)


def _build_manifest(
    *,
    job: dict[str, Any],
    file_summaries: list[dict[str, Any]],
    walk_result: WalkResult,
    elapsed_seconds: float,
) -> dict[str, Any]:
    n_files = len(file_summaries)
    total_bytes = sum(s["bytes"] for s in file_summaries)
    n_parse_errors = sum(1 for s in file_summaries if not s["parse_ok"])
    languages_count: dict[str, int] = {}
    for s in file_summaries:
        languages_count[s["language"]] = languages_count.get(s["language"], 0) + 1

    entry_points = sorted(s["path"] for s in file_summaries if s.get("has_main_guard"))
    test_files = [s["path"] for s in file_summaries if s.get("is_test")]
    config_files = [s["path"] for s in file_summaries if s.get("is_config")]
    generated_files = [s["path"] for s in file_summaries if s.get("is_generated")]

    # Derive skip counts from the per-entry `analyzed` flag rather than
    # the WalkResult.skipped list, because the lazy path mutates entries
    # post-walk (size becomes known after `git checkout` materializes
    # the batch, so an oversize entry that was originally in `included`
    # gets flipped to analyzed=False).
    all_entries = walk_result.all_entries()
    skip_buckets: dict[str, int] = {}
    for entry in all_entries:
        if not entry.analyzed:
            key = entry.skip_reason or "other"
            skip_buckets[key] = skip_buckets.get(key, 0) + 1

    return {
        "job_id": job["job_id"],
        "source": job["source"],
        "git_ref": job.get("git_ref") or "",
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "elapsed_seconds": elapsed_seconds,
        "file_count": n_files,
        "total_bytes": total_bytes,
        "tree_total": len(all_entries),
        "skipped_count": sum(1 for e in all_entries if not e.analyzed),
        "skipped_by_reason": skip_buckets,
        "languages": languages_count,
        "parse_errors_count": n_parse_errors,
        "entry_points": entry_points,
        "test_file_count": len(test_files),
        "config_file_count": len(config_files),
        "generated_file_count": len(generated_files),
    }


def _build_languages(file_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    by_lang: dict[str, dict[str, int]] = {}
    for s in file_summaries:
        lang = s["language"] or "unknown"
        bucket = by_lang.setdefault(lang, {"file_count": 0, "bytes": 0, "loc": 0})
        bucket["file_count"] += 1
        bucket["bytes"] += s["bytes"]
        bucket["loc"] += s["loc"]
    return {"languages": by_lang}


def _build_symbols_index(
    output_dir: Path,
    file_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Read each per-file artifact and emit a global qualified_name -> location index.

    The index is structured as a list rather than a map because qualified
    names *can* collide across languages within a polyglot repo (think
    `main.foo` in two unrelated Go packages). The consumer disambiguates
    using the (file, span) tuple.
    """
    files_dir = output_dir / "files"
    entries: list[dict[str, Any]] = []
    for s in file_summaries:
        artifact = files_dir / (s["path"] + ".json")
        if not artifact.exists():
            continue
        try:
            with open(artifact, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for sym in data.get("symbols", []):
            entries.append(
                {
                    "qualified_name": sym["qualified_name"],
                    "kind": sym["kind"],
                    "name": sym["name"],
                    "language": s["language"],
                    "file": s["path"],
                    "span": sym["span"],
                }
            )
    entries.sort(key=lambda e: (e["qualified_name"], e["file"]))
    return {"entries": entries}


def _build_errors(file_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [
        {
            "path": s["path"],
            "language": s["language"],
            "error_nodes": s["error_nodes"],
            "missing_nodes": s["missing_nodes"],
            "reason": s.get("parse_reason") or "",
        }
        for s in file_summaries
        if not s["parse_ok"]
    ]
    skipped = [s for s in file_summaries if s.get("skipped")]
    return {
        "parse_errors": failed,
        "skipped": skipped,
    }


def _build_tree(walk_result: WalkResult) -> dict[str, Any]:
    """Full path inventory of the repo, analyzed and skipped alike.

    The list is sorted by path so it diffs cleanly across runs and so a
    consumer can walk it in directory order without resorting.
    """
    entries = []
    for e in walk_result.all_entries():
        item: dict[str, Any] = {
            "path": e.path,
            "size": e.size,
            "analyzed": e.analyzed,
        }
        if e.skip_reason:
            item["skip_reason"] = e.skip_reason
        entries.append(item)
    return {"entries": entries}


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)
