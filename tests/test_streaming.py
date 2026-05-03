"""Tests for the streaming-fetch path: bin-packing, walk_from_inventory,
and a full end-to-end run with mocked GitHub HTTP calls."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

from parkview_codeparse import jobs, walker

# ---------------------------------------------------------------------------
# walker.walk_from_inventory


def test_walk_from_inventory_basic():
    sizes = {"a.py": 10, "b.py": 20, "c.md": 30}
    r = walker.walk_from_inventory(sizes=sizes, max_file_bytes=1000)
    by = {e.path: e for e in r.included}
    assert set(by) == {"a.py", "b.py", "c.md"}
    assert all(e.size == sizes[e.path] for e in r.included)


def test_walk_from_inventory_default_dir_skip_silent():
    sizes = {
        "src/keep.py": 10,
        "node_modules/leftpad/index.js": 20,
        ".git/HEAD": 30,
    }
    r = walker.walk_from_inventory(sizes=sizes, max_file_bytes=1000)
    paths_included = {e.path for e in r.included}
    paths_skipped = {e.path for e in r.skipped}
    # Directory-level skips don't appear at all (neither included nor skipped) —
    # the doc on walk_full says they'd flood tree.json otherwise.
    assert paths_included == {"src/keep.py"}
    assert paths_skipped == set()


def test_walk_from_inventory_size_filter():
    sizes = {"small.py": 100, "huge.py": 5_000_000}
    r = walker.walk_from_inventory(sizes=sizes, max_file_bytes=1_000_000)
    included = {e.path for e in r.included}
    skipped = {e.path: e.skip_reason for e in r.skipped}
    assert included == {"small.py"}
    assert skipped == {"huge.py": "oversize"}


def test_walk_from_inventory_binary_extension_filter():
    sizes = {"code.py": 10, "image.png": 20, "icon.ico": 30}
    r = walker.walk_from_inventory(sizes=sizes, max_file_bytes=1000)
    included = {e.path for e in r.included}
    skipped = {e.path: e.skip_reason for e in r.skipped}
    assert included == {"code.py"}
    assert skipped == {"image.png": "binary", "icon.ico": "binary"}


def test_walk_from_inventory_gitignore_filter():
    sizes = {"keep.py": 10, "ignore_me.py": 20, "logs/run.log": 30}
    r = walker.walk_from_inventory(
        sizes=sizes,
        gitignore_text="ignore_me.py\nlogs/\n",
    )
    included = {e.path for e in r.included}
    skipped = {e.path: e.skip_reason for e in r.skipped}
    assert included == {"keep.py"}
    assert skipped == {"ignore_me.py": "gitignore", "logs/run.log": "gitignore"}


def test_walk_from_inventory_extra_globs():
    sizes = {"keep.py": 10, "internal/secret.py": 20}
    r = walker.walk_from_inventory(sizes=sizes, extra_ignore_globs=["internal/"])
    included = {e.path for e in r.included}
    skipped = {e.path: e.skip_reason for e in r.skipped}
    assert included == {"keep.py"}
    assert skipped == {"internal/secret.py": "extra_ignore"}


# ---------------------------------------------------------------------------
# jobs._bin_pack


def _entry(path: str, size: int) -> walker.TreeEntry:
    return walker.TreeEntry(path=path, size=size, analyzed=True)


def test_bin_pack_fits_in_one_batch():
    entries = [_entry(f"f{i}.py", 1000) for i in range(5)]
    batches = jobs._bin_pack(entries, limit=10_000)
    assert len(batches) == 1
    assert sum(e.size for e in batches[0]) <= 10_000


def test_bin_pack_splits_when_over_limit():
    entries = [_entry(f"f{i}.py", 600) for i in range(10)]
    batches = jobs._bin_pack(entries, limit=1000)
    assert all(sum(e.size for e in b) <= 1000 for b in batches)
    # FFD with 10 600-byte items into 1000-byte bins: each bin holds one item.
    assert len(batches) == 10


def test_bin_pack_oversize_single_file_gets_own_batch():
    entries = [_entry("small.py", 100), _entry("huge.py", 5_000_000)]
    batches = jobs._bin_pack(entries, limit=1_000_000)
    assert any(b == [entries[1]] for b in batches)
    # Total file count preserved.
    assert sum(len(b) for b in batches) == 2


def test_bin_pack_first_fit_decreasing_packs_efficiently():
    # 700 + 300 fits in one bin; 400 + 200 + 100 fits in another. With a
    # naive first-fit you'd get more bins. FFD packs to 2.
    entries = [_entry("a", 700), _entry("b", 400), _entry("c", 300), _entry("d", 200), _entry("e", 100)]
    batches = jobs._bin_pack(entries, limit=1000)
    assert len(batches) == 2


def test_bin_pack_empty_input():
    assert jobs._bin_pack([], limit=1000) == []


# ---------------------------------------------------------------------------
# Streaming end-to-end (mocked github)


_FAKE_FILES = {
    "alpha.py": '"""alpha."""\n\ndef hello(): return 1\n',
    "pkg/beta.py": "class Beta:\n    def m(self): return 2\n",
    "README.md": "# Demo\n",
    "image.png": "\x89PNG\x00stub",  # would be skipped as binary
    "huge.py": "x" * 5000,  # would be skipped as oversize
}


def _fake_blob_sizes(*args, **kwargs):
    return {p: len(c.encode()) for p, c in _FAKE_FILES.items()}


def _fake_default_branch(*args, **kwargs):
    return "main"


def _fake_blob_via_raw(owner, repo, ref, rel_path, *, token=None, timeout=60.0):
    if rel_path == ".gitignore":
        return None  # no .gitignore in our fake repo
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
    raise AssertionError(f"job {job_id} did not finish")


def test_streaming_end_to_end_with_mocked_github(tmp_path: Path):
    """Force streaming mode via a tiny `max_partial_clone_bytes` and verify
    everything wires together: planning, batching, raw fetch, analyze,
    delete, manifest + tree.json."""
    out = tmp_path / "out"

    with (
        patch.object(jobs.github, "fetch_blob_sizes", side_effect=_fake_blob_sizes),
        patch.object(jobs.github, "fetch_default_branch", side_effect=_fake_default_branch),
        patch.object(jobs.github, "fetch_blob_via_raw", side_effect=_fake_blob_via_raw),
    ):
        job_id = jobs.start_index_repo(
            {
                "source": "https://github.com/fake/repo",
                "output_dir": str(out),
                # Force streaming: cap at 50 bytes -> our two .py files (>50 each)
                # will land in separate batches.
                "max_partial_clone_bytes": 50,
                # Tighten the per-file cap so the 5000-byte "huge.py" trips
                # the oversize filter inside walk_from_inventory.
                "max_file_bytes": 1000,
            }
        )
        snap = _wait_done(job_id)

    assert snap["state"] == "done", f"snap: {snap}"
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["file_count"] >= 2  # alpha.py and pkg/beta.py at minimum
    assert "python" in manifest["languages"]

    # The streaming path materialized files briefly under .source/ then
    # deleted them; .source/ should exist but be empty (or just empty dirs).
    src_dir = out / ".source"
    assert src_dir.is_dir()
    leftover_files = [p for p in src_dir.rglob("*") if p.is_file()]
    assert leftover_files == [], f"expected scratch to be empty, got: {leftover_files}"

    # tree.json contains every path the Trees API returned, including the
    # ones we deliberately skipped (binary, oversize).
    tree = json.loads((out / "tree.json").read_text())
    by_path = {e["path"]: e for e in tree["entries"]}
    assert by_path["alpha.py"]["analyzed"] is True
    assert by_path["pkg/beta.py"]["analyzed"] is True
    assert by_path["image.png"]["analyzed"] is False
    assert by_path["image.png"]["skip_reason"] == "binary"
    assert by_path["huge.py"]["analyzed"] is False
    assert by_path["huge.py"]["skip_reason"] == "oversize"

    # log.jsonl recorded the streaming-mode events.
    log_events = [json.loads(ln) for ln in (out / "log.jsonl").read_text().splitlines() if ln.strip()]
    event_types = {e["event"] for e in log_events}
    assert "github_trees_api_ok" in event_types
    assert "batches_planned" in event_types
    assert "batch_start" in event_types
    assert "batch_done" in event_types
    assert "manifest_written" in event_types

    # The streaming path bypassed the normal source_resolved-with-clone
    # event; it emits source_resolved with mode=streaming.
    src_event = next(e for e in log_events if e["event"] == "source_resolved")
    assert src_event["mode"] == "streaming"


def test_streaming_not_used_when_below_threshold(tmp_path: Path):
    """Below-threshold GitHub jobs should still take the single-clone path."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.py").write_text("x = 1\n")
    out = tmp_path / "out"

    # No mocking — local source path takes the single-clone branch.
    job_id = jobs.start_index_repo(
        {
            "source": str(src),
            "output_dir": str(out),
        }
    )
    snap = _wait_done(job_id)
    assert snap["state"] == "done"
    log_events = [json.loads(ln) for ln in (out / "log.jsonl").read_text().splitlines() if ln.strip()]
    src_event = next(e for e in log_events if e["event"] == "source_resolved")
    assert src_event["mode"] == "single"
