"""Tests for the retention sweeper.

`sweep_once` is synchronous and pure-ish (only side-effect: rmtree on
expired dirs + drop from jobs table), so we drive it directly without
spinning up the asyncio loop.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from deco_assaying import config, jobs, retention


@pytest.fixture
def output_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "output"
    root.mkdir()
    monkeypatch.setattr(config, "OUTPUT_ROOT", root)
    return root


def _backdate(path: Path, days: int) -> None:
    """Set mtime to `days` ago."""
    past = time.time() - days * 86400
    os.utime(path, (past, past))


def test_sweep_removes_expired_dirs(output_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "OUTPUT_EXPIRY_DAYS", 7)

    fresh = output_root / "fresh-job"
    fresh.mkdir()
    (fresh / "manifest.json").write_text("{}")

    stale = output_root / "stale-job"
    stale.mkdir()
    (stale / "manifest.json").write_text("{}")
    _backdate(stale, days=10)

    removed = retention.sweep_once()
    assert removed == ["stale-job"]
    assert fresh.exists()
    assert not stale.exists()


def test_sweep_skips_active_jobs(output_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "OUTPUT_EXPIRY_DAYS", 7)

    job_id = "running-job"
    job_dir = output_root / job_id
    job_dir.mkdir()
    _backdate(job_dir, days=30)

    fake_job = {
        "job_id": job_id,
        "source": "fake",
        "output_path": str(job_dir),
        "git_ref": "",
        "options": {},
        "status": "running",
        "files_done": 0,
        "files_total": 0,
        "errors_count": 0,
        "started_at": time.time(),
        "finished_at": None,
        "manifest_path": None,
        "log_path": None,
        "error": None,
        "_cancel": False,
    }
    with jobs._lock:
        jobs._jobs[job_id] = fake_job
    try:
        removed = retention.sweep_once()
        assert job_id not in removed
        assert job_dir.exists()
    finally:
        with jobs._lock:
            jobs._jobs.pop(job_id, None)


def test_sweep_disabled_when_expiry_zero(output_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "OUTPUT_EXPIRY_DAYS", 0)
    stale = output_root / "stale"
    stale.mkdir()
    _backdate(stale, days=365)

    removed = retention.sweep_once()
    assert removed == []
    assert stale.exists()


def test_sweep_drops_table_entry_on_removal(output_root: Path, monkeypatch: pytest.MonkeyPatch):
    """A done job whose dir aged out should also disappear from the
    in-memory job table — otherwise /admin/jobs would point at a
    vanished output_path."""
    monkeypatch.setattr(config, "OUTPUT_EXPIRY_DAYS", 7)

    job_id = "done-job"
    job_dir = output_root / job_id
    job_dir.mkdir()
    _backdate(job_dir, days=30)

    fake_job = {
        "job_id": job_id,
        "source": "fake",
        "output_path": str(job_dir),
        "git_ref": "",
        "options": {},
        "status": "done",
        "files_done": 1,
        "files_total": 1,
        "errors_count": 0,
        "started_at": time.time() - 30 * 86400,
        "finished_at": time.time() - 30 * 86400,
        "manifest_path": str(job_dir / "manifest.json"),
        "log_path": None,
        "error": None,
        "_cancel": False,
    }
    with jobs._lock:
        jobs._jobs[job_id] = fake_job
    try:
        retention.sweep_once()
        assert not job_dir.exists()
        assert jobs.get_status(job_id) is None
    finally:
        with jobs._lock:
            jobs._jobs.pop(job_id, None)


def test_sweep_handles_missing_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """If OUTPUT_ROOT doesn't exist (fresh deploy), sweep is a no-op."""
    monkeypatch.setattr(config, "OUTPUT_EXPIRY_DAYS", 7)
    monkeypatch.setattr(config, "OUTPUT_ROOT", tmp_path / "does-not-exist")
    assert retention.sweep_once() == []
