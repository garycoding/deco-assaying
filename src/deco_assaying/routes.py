"""FastAPI routes + MCP server: /health, /admin/*, /sse, MCP tools."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel

from deco_assaying import analyze, jobs, languages, outputs, retention
from deco_assaying.config import JOB_HISTORY_MAX, VERSION


def _safe_pkg_version(name: str) -> str:
    try:
        return _pkg_version(name)
    except PackageNotFoundError:
        return "unknown"


logger = logging.getLogger(__name__)
_started_at = time.time()

router = APIRouter()

# ---------------------------------------------------------------------------
# MCP server (mounted at /sse via app.py)

mcp = Server("deco-assaying", version=VERSION)
session_manager = StreamableHTTPSessionManager(app=mcp, stateless=True)


@mcp.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="analyze_file",
            description=(
                "Analyze a single source file passed inline as text. Returns "
                "structured JSON: file metadata, symbols, imports, exports, "
                "outgoing references, literals of interest, AST-aware chunks, "
                "metrics, and parse status. The server does not read from disk."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Source code as a UTF-8 string.",
                    },
                    "filename": {
                        "type": "string",
                        "description": (
                            "Filename or relative path. Used for language "
                            "detection and the file.path field. No filesystem "
                            "access is performed."
                        ),
                        "default": "",
                    },
                    "language": {
                        "type": "string",
                        "description": (
                            "Override language detection. Use list_supported_languages to see valid ids."
                        ),
                        "default": "",
                    },
                    "include_chunks": {
                        "type": "boolean",
                        "description": "Include AST-aware chunks in the response.",
                        "default": True,
                    },
                    "chunk_max_tokens": {
                        "type": "integer",
                        "description": "Approximate max token count per chunk.",
                        "default": 800,
                    },
                },
                "required": ["content"],
            },
        ),
        types.Tool(
            name="index_repo",
            description=(
                "Start an asynchronous job that indexes a repository (local "
                "path, GitHub URL, or GitLab URL) and writes per-file JSON "
                "artifacts plus a manifest.json. The server allocates a "
                "fresh output directory under OUTPUT_ROOT (default ./output "
                "for the daemon, /data in the docker image) and returns "
                "the absolute path in `output_path`. Poll get_job_status "
                "or watch output_path/log.jsonl."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": (
                            "Local directory path, GitHub URL "
                            "(https://github.com/owner/repo), or GitLab URL "
                            "(https://gitlab.com/owner/repo, including nested "
                            "groups like https://gitlab.com/group/sub/repo). "
                            "Set GITHUB_TOKEN or GITLAB_TOKEN to authenticate "
                            "and access private repos."
                        ),
                    },
                    "git_ref": {
                        "type": "string",
                        "description": (
                            "Branch, tag, or sha to analyze. Applies to "
                            "GitHub and GitLab sources; ignored for local "
                            "paths. Defaults to the repo's default branch."
                        ),
                        "default": "",
                    },
                    "respect_gitignore": {"type": "boolean", "default": True},
                    "extra_ignore_globs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                    "max_file_bytes": {
                        "type": "integer",
                        "default": 2 * 1024 * 1024,
                        "description": (
                            "Per-file size cap. For GitHub URLs this is also "
                            "passed to git as --filter=blob:limit=, so blobs "
                            "above this size never get fetched."
                        ),
                    },
                    "eager_clone": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, GitHub URLs use a full --depth=1 clone "
                            "(every blob fetched) instead of the size-bounded "
                            "partial clone. Faster on small repos; downloads "
                            "every binary on big ones."
                        ),
                    },
                    "provider_api": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Optional pre-flight against the source's "
                            "hosting provider (GitHub Trees REST or GitLab "
                            "REST tree + GraphQL) to enumerate every blob "
                            "with its size, without fetching contents. "
                            "Drives the streaming / single-clone decision "
                            "and fills in tree.json sizes for unfetched "
                            "files. Silent fallback on rate-limit / failure. "
                            "Set GITHUB_TOKEN or GITLAB_TOKEN to "
                            "authenticate. Accepts the legacy `github_api` "
                            "name for back-compat."
                        ),
                    },
                    "max_partial_clone_bytes": {
                        "type": "integer",
                        "default": 100 * 1024 * 1024,
                        "description": (
                            "Peak source-side scratch space during a "
                            "GitHub-clone job. When the Trees API reports "
                            "the planned source download exceeds this, we "
                            "switch from a single partial clone to bin-"
                            "packed streaming: each batch of files (totaling "
                            "<= this many bytes) is fetched, analyzed, and "
                            "deleted before the next batch arrives. Lets "
                            "the server analyze multi-GB monorepos with "
                            "bounded local disk."
                        ),
                    },
                    "include_chunks": {"type": "boolean", "default": True},
                    "chunk_max_tokens": {"type": "integer", "default": 800},
                },
                "required": ["source"],
            },
        ),
        types.Tool(
            name="get_job_status",
            description="Return status, progress, and artifact paths for a job.",
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="cancel_job",
            description="Cooperatively cancel a running job.",
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="list_supported_languages",
            description="Return the list of languages this server can parse, and how fully each is supported.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="detect_language",
            description="Detect the language of a path or filename via extension/shebang heuristics. No filesystem access.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "first_line": {
                        "type": "string",
                        "description": "Optional first line of the file for shebang sniffing.",
                        "default": "",
                    },
                },
                "required": ["path"],
            },
        ),
    ]


def _ok(payload: Any) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload))]


@mcp.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "analyze_file":
            return _ok(
                analyze.analyze_inline(
                    content=arguments["content"],
                    filename=arguments.get("filename") or "",
                    language=arguments.get("language") or "",
                    include_chunks=arguments.get("include_chunks", True),
                    chunk_max_tokens=arguments.get("chunk_max_tokens", 800),
                )
            )

        if name == "index_repo":
            job_id, output_path = jobs.start_index_repo(arguments)
            return _ok({"job_id": job_id, "output_path": str(output_path)})

        if name == "get_job_status":
            snap = jobs.get_status(arguments["job_id"])
            if snap is None:
                return _ok({"error": "unknown_job_id"})
            return _ok(snap)

        if name == "cancel_job":
            return _ok({"ok": jobs.cancel(arguments["job_id"])})

        if name == "list_supported_languages":
            return _ok(languages.list_supported())

        if name == "detect_language":
            lang = languages.detect(arguments["path"], first_line=arguments.get("first_line") or "")
            return _ok({"language": lang})
    except NotImplementedError as e:
        return _ok({"error": "not_implemented", "detail": str(e)})

    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# /sse — Streamable HTTP MCP transport mounted as raw ASGI3.
# (Class instance, not bare async function, so Starlette doesn't wrap it.)


class MCPASGIApp:
    async def __call__(self, scope, receive, send) -> None:
        await session_manager.handle_request(scope, receive, send)


mcp_asgi = MCPASGIApp()


# ---------------------------------------------------------------------------
# /health


class Health(BaseModel):
    ok: bool
    version: str
    uptime_seconds: float


@router.get("/health", response_model=Health, tags=["health"])
async def health() -> Health:
    return Health(ok=True, version=VERSION, uptime_seconds=time.time() - _started_at)


# ---------------------------------------------------------------------------
# /admin/* — read-only JSON


class AdminVersion(BaseModel):
    version: str
    mcp_protocol_version: str
    tree_sitter_language_pack_version: str


class LanguageInfo(BaseModel):
    id: str
    display_name: str
    has_full_support: bool


class JobProgress(BaseModel):
    files_done: int
    files_total: int


class JobSummary(BaseModel):
    job_id: str
    source: str
    output_path: str
    state: str
    progress: JobProgress
    errors_count: int
    started_at: float
    finished_at: float | None = None


class JobDetail(JobSummary):
    manifest_path: str | None = None
    log_path: str | None = None
    error: str | None = None


class LogEvents(BaseModel):
    events: list[dict[str, Any]]
    next_offset: int


class Stats(BaseModel):
    version: str
    jobs_total: int
    jobs_done: int
    jobs_failed: int
    jobs_cancelled: int
    files_parsed_total: int
    parse_error_total: int
    files_by_language: dict[str, int]
    started_at: float


@router.get("/admin/version", response_model=AdminVersion, tags=["admin"])
async def admin_version() -> AdminVersion:
    return AdminVersion(
        version=VERSION,
        mcp_protocol_version=_safe_pkg_version("mcp"),
        tree_sitter_language_pack_version=_safe_pkg_version("tree-sitter-language-pack"),
    )


@router.get("/admin/languages", response_model=list[LanguageInfo], tags=["admin"])
async def admin_languages() -> list[LanguageInfo]:
    return [LanguageInfo(**lang) for lang in languages.list_supported()]


@router.get("/admin/jobs", response_model=list[JobSummary], tags=["admin"])
async def admin_jobs(limit: int = JOB_HISTORY_MAX, status: str | None = None) -> list[JobSummary]:
    limit = max(1, min(limit, JOB_HISTORY_MAX))
    return [JobSummary(**j) for j in jobs.list_jobs(limit=limit, status=status)]


@router.get("/admin/jobs/{job_id}", response_model=JobDetail, tags=["admin"])
async def admin_job_detail(job_id: str) -> JobDetail:
    snap = jobs.get_status(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="unknown_job_id")
    return JobDetail(**snap)


@router.get("/admin/jobs/{job_id}/log", response_model=LogEvents, tags=["admin"])
async def admin_job_log(job_id: str, from_offset: int = 0, limit: int = 1000) -> LogEvents:
    limit = max(1, min(limit, 100_000))
    from_offset = max(0, from_offset)
    result = jobs.read_log(job_id, from_offset=from_offset, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="unknown_job_id")
    return LogEvents(**result)


@router.get("/admin/stats", response_model=Stats, tags=["admin"])
async def admin_stats() -> Stats:
    return Stats(version=VERSION, **jobs.stats())


# ---------------------------------------------------------------------------
# /outputs/{job_id}/... — read-only download API for finished jobs.
#
# Lets a remote consumer (no shared volume) pull artifacts without ever
# touching disk, and lets a local consumer (shared volume) skip the API
# entirely and read off disk. Either way, the on-disk layout under
# OUTPUT_ROOT/{job_id}/ is the source of truth.


class LsRow(BaseModel):
    path: str
    size: int
    mtime: float
    is_dir: bool


class LsResponse(BaseModel):
    entries: list[LsRow]


class OutputSummary(BaseModel):
    job_id: str
    size: int
    mtime: float


def _job_dir_or_404(job_id: str):
    job_dir = outputs.resolve_job_dir(job_id)
    if job_dir is None:
        raise HTTPException(status_code=404, detail="unknown_job_id")
    return job_dir


def _serve_named_json(job_id: str, name: str) -> FileResponse:
    job_dir = _job_dir_or_404(job_id)
    target = job_dir / name
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"{name} not present")
    return FileResponse(target, media_type="application/json")


@router.get("/outputs/{job_id}", tags=["outputs"])
async def outputs_root(job_id: str) -> FileResponse:
    """Convenience: same as `manifest.json`. One GET tells the consumer
    the job's shape (file count, languages, status) without any further
    chatter."""
    return _serve_named_json(job_id, "manifest.json")


@router.get("/outputs/{job_id}/manifest.json", tags=["outputs"])
async def outputs_manifest(job_id: str) -> FileResponse:
    return _serve_named_json(job_id, "manifest.json")


@router.get("/outputs/{job_id}/tree.json", tags=["outputs"])
async def outputs_tree(job_id: str) -> FileResponse:
    return _serve_named_json(job_id, "tree.json")


@router.get("/outputs/{job_id}/symbols.json", tags=["outputs"])
async def outputs_symbols(job_id: str) -> FileResponse:
    return _serve_named_json(job_id, "symbols.json")


@router.get("/outputs/{job_id}/languages.json", tags=["outputs"])
async def outputs_languages(job_id: str) -> FileResponse:
    return _serve_named_json(job_id, "languages.json")


@router.get("/outputs/{job_id}/errors.json", tags=["outputs"])
async def outputs_errors(job_id: str) -> FileResponse:
    return _serve_named_json(job_id, "errors.json")


@router.get("/outputs/{job_id}/log.jsonl", response_model=LogEvents, tags=["outputs"])
async def outputs_log(job_id: str, from_offset: int = 0, limit: int = 1000) -> LogEvents:
    """Same shape as `/admin/jobs/{id}/log` — re-exposed under /outputs
    so a consumer treating the artifact dir as the source of truth doesn't
    need to know about /admin."""
    limit = max(1, min(limit, 100_000))
    from_offset = max(0, from_offset)
    result = jobs.read_log(job_id, from_offset=from_offset, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="unknown_job_id")
    return LogEvents(**result)


@router.get("/outputs/{job_id}/ls", response_model=LsResponse, tags=["outputs"])
async def outputs_ls(job_id: str, path: str = "", recursive: bool = False) -> LsResponse:
    """Directory listing rooted at `path` (relative to the job dir).

    With `recursive=true`, walks the whole subtree. Each row reports
    relative path, byte size (0 for dirs), mtime, and is_dir.
    """
    job_dir = _job_dir_or_404(job_id)
    try:
        sub = outputs.safe_subpath(job_dir, path)
    except outputs.OutputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not sub.exists():
        raise HTTPException(status_code=404, detail="path not found")
    try:
        rows = outputs.list_dir(job_dir, sub, recursive=recursive)
    except outputs.OutputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return LsResponse(entries=[LsRow(**r) for r in rows])


@router.get("/outputs/{job_id}/file/{path:path}", tags=["outputs"])
async def outputs_file(job_id: str, path: str):
    """Content-aware download. If the last segment is a glob (`*?[`),
    expands it under the parent dir and streams a ZIP of the matches.
    Otherwise serves the single file."""
    job_dir = _job_dir_or_404(job_id)
    last = path.rsplit("/", 1)[-1]
    if any(outputs.is_glob(seg) for seg in path.split("/")):
        # A glob anywhere in the path → ZIP of matches. We don't try to
        # validate "the parent dir exists" because patterns like
        # `files/**/*.py.json` deliberately have `**` in the parent.
        # expand_glob() rooted at job_dir provides the safety boundary.
        files = outputs.expand_glob(job_dir, path)
        return StreamingResponse(
            outputs.stream_zip(job_dir, files),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{job_id}-{last}.zip"',
            },
        )
    try:
        target = outputs.safe_subpath(job_dir, path)
    except outputs.OutputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not target.exists():
        raise HTTPException(status_code=404, detail="not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not a file")
    return FileResponse(target)


@router.get("/outputs/{job_id}/zip", tags=["outputs"])
async def outputs_zip(job_id: str, path: str = "", match: str = "**/*"):
    """Explicit-bulk-zip alias. Defaults to the whole job dir.

    `path` selects a subdirectory; `match` is a glob applied beneath
    it (default `**/*`). Streams `application/zip`.
    """
    job_dir = _job_dir_or_404(job_id)
    try:
        sub = outputs.safe_subpath(job_dir, path)
    except outputs.OutputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not sub.is_dir():
        raise HTTPException(status_code=404, detail="path not found")
    # Build the pattern relative to job_dir (Path.glob is rooted there).
    sub_rel = "" if sub == job_dir else str(sub.relative_to(job_dir))
    pattern = f"{sub_rel}/{match}" if sub_rel else match
    files = outputs.expand_glob(job_dir, pattern)
    return StreamingResponse(
        outputs.stream_zip(job_dir, files),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{job_id}.zip"'},
    )


@router.delete("/outputs/{job_id}", status_code=204, tags=["outputs"])
async def outputs_delete(job_id: str) -> Response:
    """Remove the job's output dir and drop its row from the in-memory
    table. Refuses to delete an active (non-terminal) job."""
    if jobs.is_active(job_id):
        raise HTTPException(status_code=409, detail="job is still running; cancel first")
    job_dir = outputs.resolve_job_dir(job_id)
    dropped = jobs.drop(job_id)
    if job_dir is None and not dropped:
        raise HTTPException(status_code=404, detail="unknown_job_id")
    if job_dir is not None:
        try:
            outputs.remove_job_dir(job_dir)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"removal failed: {e}") from e
    return Response(status_code=204)


@router.get("/admin/outputs", response_model=list[OutputSummary], tags=["admin"])
async def admin_outputs() -> list[OutputSummary]:
    """List every job_id present on disk under OUTPUT_ROOT, with size +
    mtime. Includes jobs that have aged out of the in-memory table."""
    return [OutputSummary(**row) for row in outputs.list_outputs_root()]


# ---------------------------------------------------------------------------
# Lifespan — runs the MCP session manager.


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    uvlog = logging.getLogger("uvicorn.error")
    async with session_manager.run():
        sweeper = asyncio.create_task(retention.run_forever(), name="retention-sweeper")
        uvlog.info("deco-assaying v%s ready", VERSION)
        try:
            yield
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
