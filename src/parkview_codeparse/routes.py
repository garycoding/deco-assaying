"""FastAPI routes + MCP server: /health, /admin/*, /sse, MCP tools."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel

from parkview_codeparse import analyze, jobs, languages
from parkview_codeparse.config import JOB_HISTORY_MAX, VERSION


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

mcp = Server("parkview-codeparse-server", version=VERSION)
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
                "path or GitHub URL) and writes per-file JSON artifacts plus "
                "a manifest.json under output_dir. Returns a job_id; poll "
                "get_job_status or watch output_dir/log.jsonl."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Local directory path OR a GitHub URL (https://github.com/owner/repo).",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Absolute path to the directory where artifacts will be written.",
                    },
                    "git_ref": {
                        "type": "string",
                        "description": "Branch, tag, or sha to clone (GitHub sources only).",
                        "default": "",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "If true, overwrite a non-empty output_dir.",
                        "default": False,
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
                    "github_api": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Optional pre-flight: hit the GitHub Trees API "
                            "for accurate blob sizes (no blob fetches). Used "
                            "to fill in real sizes for files we don't fetch. "
                            "Silent fallback on rate-limit / failure. Set "
                            "GITHUB_TOKEN env to authenticate (60/hr "
                            "unauthenticated, 5000/hr with token)."
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
                "required": ["source", "output_dir"],
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
            job_id = jobs.start_index_repo(arguments)
            return _ok({"job_id": job_id, "output_dir": arguments["output_dir"]})

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
    output_dir: str
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
    return Stats(**jobs.stats())


# ---------------------------------------------------------------------------
# Lifespan — runs the MCP session manager.


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    uvlog = logging.getLogger("uvicorn.error")
    async with session_manager.run():
        uvlog.info("parkview-codeparse-server v%s ready", VERSION)
        yield
