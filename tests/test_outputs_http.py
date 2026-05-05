"""Tests for the `/outputs/{job_id}/...` download API.

Drives the running FastAPI app via TestClient. Each test runs a small
local-source index_repo job and then exercises the read endpoints
against the resulting output dir.
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deco_assaying import config, jobs
from deco_assaying.app import app


@pytest.fixture(scope="module")
def client():
    # No `with` — we don't need the MCP /sse lifespan started up here, and
    # entering it twice across test modules trips StreamableHTTPSessionManager's
    # "only run once" guard.
    return TestClient(app)


@pytest.fixture
def output_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "output"
    monkeypatch.setattr(config, "OUTPUT_ROOT", root)
    return root


def _wait_done(job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = jobs.get_status(job_id)
        assert snap is not None
        if snap["state"] in ("done", "failed", "cancelled"):
            return snap
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


@pytest.fixture
def finished_job(tmp_path: Path, output_root: Path) -> tuple[str, Path]:
    """Index a tiny local repo and yield the (job_id, output_path)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "alpha.py").write_text('"""alpha."""\n\ndef hello(): return 1\n')
    (src / "pkg").mkdir()
    (src / "pkg" / "beta.py").write_text("class Beta:\n    pass\n")
    (src / "main.go").write_text("package main\n\nfunc main() {}\n")
    job_id, out = jobs.start_index_repo({"source": str(src)})
    snap = _wait_done(job_id)
    assert snap["state"] == "done"
    return job_id, out


# ---------------------------------------------------------------------------
# Direct file endpoints


def test_outputs_root_serves_manifest(client: TestClient, finished_job):
    job_id, _ = finished_job
    r = client.get(f"/outputs/{job_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["file_count"] >= 2
    assert "python" in body["languages"]


def test_outputs_named_files(client: TestClient, finished_job):
    job_id, _ = finished_job
    for name in (
        "manifest.json",
        "tree.json",
        "all_symbols.json",
        "top_level_symbols.json",
        "languages.json",
        "analysis_index.json",
    ):
        r = client.get(f"/outputs/{job_id}/{name}")
        assert r.status_code == 200, f"{name}: {r.status_code} {r.text[:200]!r}"
        assert r.headers["content-type"].startswith("application/json")


def test_outputs_log(client: TestClient, finished_job):
    job_id, _ = finished_job
    r = client.get(f"/outputs/{job_id}/log.jsonl")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["events"], list)
    assert body["next_offset"] > 0
    events = {e["event"] for e in body["events"]}
    assert "manifest_written" in events


def test_outputs_gzip_when_client_opts_in(client: TestClient, finished_job):
    """Client sends Accept-Encoding: gzip → server responds with
    Content-Encoding: gzip for any JSON ≥ 1 KB. The per-file
    analysis for alpha.py includes the full `chunks` payload which
    comfortably exceeds the threshold."""
    job_id, _ = finished_job
    r = client.get(
        f"/outputs/{job_id}/file/files/alpha.py.json",
        headers={"Accept-Encoding": "gzip"},
    )
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"


def test_outputs_no_gzip_when_client_does_not_opt_in(client: TestClient, finished_job):
    """No Accept-Encoding header → no gzip layer (Starlette's
    GZipMiddleware respects the client's preferences)."""
    job_id, _ = finished_job
    r = client.get(
        f"/outputs/{job_id}/file/files/alpha.py.json",
        headers={"Accept-Encoding": "identity"},
    )
    assert r.status_code == 200
    assert r.headers.get("content-encoding") != "gzip"


def test_outputs_unknown_job_404(client: TestClient, output_root: Path):
    r = client.get("/outputs/does-not-exist")
    assert r.status_code == 404
    assert r.json()["detail"] == "unknown_job_id"

    r = client.get("/outputs/does-not-exist/manifest.json")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /ls


def test_ls_root(client: TestClient, finished_job):
    job_id, _ = finished_job
    r = client.get(f"/outputs/{job_id}/ls")
    assert r.status_code == 200
    paths = {row["path"] for row in r.json()["entries"]}
    assert "manifest.json" in paths
    assert "files" in paths


def test_ls_recursive(client: TestClient, finished_job):
    job_id, _ = finished_job
    r = client.get(f"/outputs/{job_id}/ls", params={"path": "files", "recursive": True})
    assert r.status_code == 200
    paths = {row["path"] for row in r.json()["entries"]}
    assert "files/alpha.py.json" in paths
    assert "files/pkg/beta.py.json" in paths


def test_ls_path_traversal_rejected(client: TestClient, finished_job):
    job_id, _ = finished_job
    r = client.get(f"/outputs/{job_id}/ls", params={"path": "../"})
    # Either 400 (resolved-but-escapes) or 404 (resolved-but-missing).
    assert r.status_code in (400, 404)


# ---------------------------------------------------------------------------
# /file/{path} — single file


def test_file_single(client: TestClient, finished_job):
    job_id, out = finished_job
    r = client.get(f"/outputs/{job_id}/file/files/alpha.py.json")
    assert r.status_code == 200
    assert r.json()["file"]["language"] == "python"
    # Same bytes as on disk.
    on_disk = (out / "files" / "alpha.py.json").read_bytes()
    assert r.content == on_disk


def test_file_traversal_rejected(client: TestClient, finished_job):
    job_id, _ = finished_job
    # FastAPI normalizes some `..` in the URL, so try a few shapes.
    for path in (
        "../../../etc/passwd",
        "files/../../../etc/passwd",
    ):
        r = client.get(f"/outputs/{job_id}/file/{path}")
        assert r.status_code in (400, 404)


def test_file_404_missing(client: TestClient, finished_job):
    job_id, _ = finished_job
    r = client.get(f"/outputs/{job_id}/file/files/does-not-exist.json")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /file/{path} — glob → streaming ZIP


def test_file_glob_streams_zip(client: TestClient, finished_job):
    job_id, out = finished_job
    r = client.get(f"/outputs/{job_id}/file/files/**/*.py.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert "files/alpha.py.json" in names
    assert "files/pkg/beta.py.json" in names
    # ZIP body matches what's on disk.
    assert zf.read("files/alpha.py.json") == (out / "files" / "alpha.py.json").read_bytes()


def test_file_glob_top_level(client: TestClient, finished_job):
    job_id, _ = finished_job
    r = client.get(f"/outputs/{job_id}/file/*.json")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    # All top-level json rollups land here.
    assert "manifest.json" in names
    assert "tree.json" in names


# ---------------------------------------------------------------------------
# /zip — explicit alias


def test_zip_whole_job(client: TestClient, finished_job):
    job_id, _ = finished_job
    r = client.get(f"/outputs/{job_id}/zip")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert "manifest.json" in names
    assert "files/alpha.py.json" in names


def test_zip_match_filter(client: TestClient, finished_job):
    job_id, _ = finished_job
    r = client.get(f"/outputs/{job_id}/zip", params={"path": "files", "match": "**/*.py.json"})
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert "files/alpha.py.json" in names
    # match scoped to files/, so manifest.json is not in.
    assert "manifest.json" not in names


# ---------------------------------------------------------------------------
# DELETE


def test_delete_removes_dir_and_table_entry(client: TestClient, finished_job):
    job_id, out = finished_job
    assert out.is_dir()
    r = client.delete(f"/outputs/{job_id}")
    assert r.status_code == 204
    assert not out.exists()
    # Subsequent lookups 404.
    assert client.get(f"/outputs/{job_id}").status_code == 404
    assert client.get(f"/admin/jobs/{job_id}").status_code == 404


def test_delete_unknown_404(client: TestClient, output_root: Path):
    r = client.delete("/outputs/does-not-exist")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /admin/outputs


def test_admin_outputs_lists_on_disk_jobs(client: TestClient, finished_job):
    job_id, _ = finished_job
    r = client.get("/admin/outputs")
    assert r.status_code == 200
    rows = r.json()
    job_ids = {row["job_id"] for row in rows}
    assert job_id in job_ids
    row = next(row for row in rows if row["job_id"] == job_id)
    assert row["size"] > 0
    assert row["mtime"] > 0


# ---------------------------------------------------------------------------
# Active job protection


def test_delete_refuses_active_job(client: TestClient, tmp_path: Path, output_root: Path):
    """Forge an in-table job in `running` state; DELETE should 409."""
    job_id = "deadbeef" + "0" * 8
    out = output_root / job_id
    out.mkdir(parents=True)
    fake_job = {
        "job_id": job_id,
        "source": "fake",
        "output_path": str(out),
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
        r = client.delete(f"/outputs/{job_id}")
        assert r.status_code == 409
        assert out.exists()
    finally:
        with jobs._lock:
            jobs._jobs.pop(job_id, None)
