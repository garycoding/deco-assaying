"""Repo-level rollups: manifest.json, all_symbols.json,
top_level_symbols.json, languages.json, errors.json, tree.json,
plus the analysis_index.json sidecar that lists every artifact
with its byte size and absolute download URL.

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
    """Write every rollup atomically.

    Order matters. We want the artifact-size index to know about
    *every* artifact — including manifest.json — so consumers can
    iterate it as a complete catalog. So:

      1. Write the smaller rollups (languages, both symbols variants,
         errors, tree).
      2. Write `manifest.json` (so the index can stat it).
      3. Build and write `analysis_index.json` last. Its presence
         now serves as the completion sentinel — and because it
         depends on every other artifact existing first, it's
         strictly more reliable for that than manifest used to be.
    """
    manifest_payload = _build_manifest(
        job=job,
        file_summaries=file_summaries,
        walk_result=walk_result,
        elapsed_seconds=elapsed_seconds,
    )
    languages = _build_languages(file_summaries)
    all_symbols = _build_symbols_index(output_dir, file_summaries)
    top_level_symbols = _filter_top_level_symbols(all_symbols)
    errors = _build_errors(file_summaries)
    tree = _build_tree(walk_result)

    # 1. Smaller rollups.
    _write_atomic(output_dir / "languages.json", languages)
    _write_atomic(output_dir / "all_symbols.json", all_symbols)
    _write_atomic(output_dir / "top_level_symbols.json", top_level_symbols)
    _write_atomic(output_dir / "errors.json", errors)
    _write_atomic(output_dir / "tree.json", tree)

    # 2. manifest.json — built before the index so the index can
    # stat it and include it in the artifact list.
    _write_atomic(output_dir / "manifest.json", manifest_payload)

    # 3. analysis_index.json last — it's the completion sentinel
    # and it lists every artifact in the dir (itself included, with
    # size_bytes=null since we'd recurse on our own size).
    analysis_index = _build_analysis_index(output_dir, job["job_id"])
    _write_atomic(output_dir / "analysis_index.json", analysis_index)


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


def _filter_top_level_symbols(all_symbols: dict[str, Any]) -> dict[str, Any]:
    """Drop entries that aren't module-level: anything dotted in the
    qualified name (methods, nested classes, etc.) plus the synthetic
    `module` rollup symbols some analyzers emit per file.

    The cheap top-level-only view a context-window-conscious agent
    reaches for first.
    """
    entries = [
        e
        for e in all_symbols.get("entries", [])
        if "." not in e["qualified_name"] and e.get("kind") != "module"
    ]
    return {"entries": entries}


def _build_analysis_index(output_dir: Path, job_id: str) -> dict[str, Any]:
    """Stat every artifact in the job dir and return an index of
    `{name, kind, size_bytes, url}` rows plus a bundle ZIP entry.

    URLs are absolute, built from `config.PUBLIC_BASE_URL`. The
    download API serves rollups/logs at `/outputs/{job_id}/<name>`
    and per-file artifacts at `/outputs/{job_id}/file/<rel>`.
    """
    # Imported here (not at module top) to avoid a config-import cycle
    # and keep this module's leaf-ness clean for fixtures that build
    # WalkResult without setting up config.
    from deco_assaying import config

    base = config.PUBLIC_BASE_URL.rstrip("/")
    job_url = f"{base}/outputs/{job_id}"

    # Top-level rollups + log — the artifacts served at /outputs/{id}/<name>.
    # `manifest.json` is included because we build this index *after*
    # manifest is written (see the order documented in `write`).
    rollup_names = [
        "manifest.json",
        "tree.json",
        "all_symbols.json",
        "top_level_symbols.json",
        "languages.json",
        "errors.json",
        "log.jsonl",
    ]
    artifacts: list[dict[str, Any]] = []
    for name in rollup_names:
        path = output_dir / name
        if not path.is_file():
            # `log.jsonl` may not exist if the job had nothing to log
            # (rare). Everything else is guaranteed to be there.
            continue
        artifacts.append(
            {
                "name": name,
                "kind": "log" if name.endswith(".jsonl") else "rollup",
                "size_bytes": path.stat().st_size,
                "url": f"{job_url}/{name}",
            }
        )

    # Self-entry. We can't stat ourselves before we exist on disk, and
    # we don't want a two-pass write because the size of the second
    # write would be slightly off from the size we recorded. So:
    # `size_bytes: null` — honest about not knowing.
    artifacts.append(
        {
            "name": "analysis_index.json",
            "kind": "rollup",
            "size_bytes": None,
            "url": f"{job_url}/analysis_index.json",
        }
    )

    # Per-file artifacts under files/.
    files_dir = output_dir / "files"
    if files_dir.is_dir():
        for path in sorted(files_dir.rglob("*.json")):
            if not path.is_file():
                continue
            rel = path.relative_to(output_dir).as_posix()  # files/src/foo.py.json
            artifacts.append(
                {
                    "name": rel,
                    "kind": "per_file",
                    "size_bytes": path.stat().st_size,
                    "url": f"{job_url}/file/{rel}",
                }
            )

    # `total_size_bytes` excludes the self-entry (which has size_bytes=null).
    total = sum(a["size_bytes"] for a in artifacts if a["size_bytes"] is not None)
    return {
        "job_id": job_id,
        "public_base_url": base,
        "outputs_path": f"/outputs/{job_id}",
        "artifacts": artifacts,
        "bundle": {
            "url": f"{job_url}/zip",
            "approx_size_bytes": None,
            "note": "ZIP is generated on demand; size predicted only after generation.",
        },
        "total_size_bytes": total,
    }


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)
