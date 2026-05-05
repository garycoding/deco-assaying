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
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel

from deco_assaying import analyze, jobs, languages, outputs, prompts, retention
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
                "Analyze ONE source file whose raw bytes you already "
                "have (e.g. the user pasted them, or you read them from "
                "a known path). Returns structured JSON: file metadata, "
                "symbols, imports, exports, outgoing references, "
                "literals of interest, AST-aware chunks, metrics, and "
                "parse status. The server does not read from disk. "
                "DO NOT use this to analyze a whole repository — call "
                "index_repo instead, then read the rollups with "
                "get_manifest, get_tree, get_top_level_symbols, etc. "
                "Never invent file contents to pass here; if you don't "
                "have actual source code in hand, don't call this tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "The raw source code, exactly as it appears "
                            "in the file. Plain text, UTF-8. NOT prose, "
                            "NOT a summary, NOT markdown describing the "
                            "code — the actual code."
                        ),
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
                "Analyze a code repository on GitHub, GitLab, or a local "
                "filesystem path. Starts an asynchronous indexing job that "
                "produces per-file JSON artifacts plus repo-level rollups "
                "(manifest, tree, symbols, languages, errors). Returns "
                "{job_id} immediately — do not wait inline. Poll "
                'get_job_status until state == "done", then read the '
                "results via the artifact-fetch tools: get_manifest, "
                "get_analysis_index (sizes + URLs for everything), "
                "get_tree, get_top_level_symbols, get_all_symbols, "
                "get_languages, get_errors, list_job_files, and "
                "get_file_analysis. Use this — not analyze_file — for "
                "any whole-repo question. See this tool's input "
                "schema for tunable parameters."
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
            description=(
                "Return state, progress, and error info for an indexing "
                'job. Poll this after index_repo until state == "done". '
                "States: pending, running, done, failed, cancelled."
            ),
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="cancel_job",
            description="Cooperatively cancel a running indexing job.",
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="get_manifest",
            description=(
                "Return the repo-level rollup for a finished indexing "
                "job. Always read this FIRST after a job completes — it "
                "summarizes file count, languages (raw counts plus a "
                "languages_by_count list pre-sorted by file count "
                "descending, so you can see the dominant language at a "
                "glance), test/config/generated buckets, parse-error "
                "count, and entry points. Small payload."
            ),
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="get_tree",
            description=(
                "Return the repo's full path inventory (analyzed + "
                "skipped). Filter to a subdirectory with path_prefix to "
                "avoid pulling the whole tree on a large repo. The "
                "response includes total_size_bytes (returned slice) "
                "and total_size_bytes_in_repo (unfiltered) so you can "
                "judge whether a subtree is worth drilling into before "
                "asking for more."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "path_prefix": {
                        "type": "string",
                        "description": (
                            "Forward-slash prefix to scope the listing "
                            '(e.g. "src/auth/"). Empty = whole tree.'
                        ),
                        "default": "",
                    },
                    "analyzed_only": {
                        "type": "boolean",
                        "description": "Drop entries that were skipped (binary/oversize/gitignore).",
                        "default": False,
                    },
                },
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="get_top_level_symbols",
            description=(
                "Return the **module-level** symbol index — only "
                "definitions at the top of each file (no methods, no "
                "nested classes, no synthetic module rollups). This "
                "is the cheap default for understanding repo shape; "
                "reach for it first. For finer-grained queries (a "
                "specific method, every nested helper) use "
                "get_all_symbols. Same response shape and the same "
                "prefix / kind / file_prefix filters as get_all_symbols; "
                "all combine with AND."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "prefix": {
                        "type": "string",
                        "description": 'Qualified-name prefix (e.g. "Auth"). Empty = no filter.',
                        "default": "",
                    },
                    "kind": {
                        "type": "string",
                        "description": (
                            "Symbol kind — one of class, interface, struct, "
                            "enum, function, constant, type_alias, decorator, "
                            "macro. (`module` and `method` won't appear here "
                            "by definition.) Empty = no filter."
                        ),
                        "default": "",
                    },
                    "file_prefix": {
                        "type": "string",
                        "description": 'Source-path prefix (e.g. "src/auth/"). Empty = no filter.',
                        "default": "",
                    },
                },
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="get_all_symbols",
            description=(
                "Return every definition across every analyzed file — "
                "module-level entries plus methods, nested classes, "
                "and synthetic module rollups. Use this for "
                'cross-cutting queries ("find every Handler", '
                '"list every method named `m`"). For repo-shape '
                "questions, prefer get_top_level_symbols — it's "
                "much cheaper. Filter by qualified-name prefix, "
                "symbol kind, and/or file-path prefix; filters AND."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "prefix": {
                        "type": "string",
                        "description": 'Qualified-name prefix (e.g. "Foo.bar"). Empty = no filter.',
                        "default": "",
                    },
                    "kind": {
                        "type": "string",
                        "description": (
                            "Symbol kind — one of module, class, interface, "
                            "struct, enum, function, method, constructor, "
                            "property, field, constant, type_alias, "
                            "decorator, macro. Empty = no filter."
                        ),
                        "default": "",
                    },
                    "file_prefix": {
                        "type": "string",
                        "description": 'Source-path prefix (e.g. "src/auth/"). Empty = no filter.',
                        "default": "",
                    },
                },
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="get_analysis_index",
            description=(
                "Return the artifact index for a finished job: every "
                "analysis file the server produced, with its byte size "
                "and an absolute download URL. Read this AFTER "
                "get_manifest to plan which artifacts to fetch — "
                "especially on large repos where some payloads exceed "
                "the context window. Each artifact's `url` lets the "
                "agent route around the prompt entirely: hand a URL "
                "to a fetch tool, process the file out-of-band, and "
                "feed only the distilled result back into the prompt. "
                "Big artifacts can therefore inform reasoning without "
                "ever overflowing context. Includes both "
                "`all_symbols.json` and `top_level_symbols.json` so "
                "the agent can size up the cheaper view at a glance."
            ),
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="get_languages",
            description="Return per-language file counts, byte sums, and lines of code. Small payload.",
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="get_errors",
            description="Return parse errors and skipped files for a job. Small payload.",
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="list_job_files",
            description=(
                "Return the source-relative paths of every per-file "
                "analysis artifact under files/. Use this to discover "
                "what's available before fetching specific files with "
                "get_file_analysis. Optional fnmatch glob filters the "
                'list (e.g. "src/**/*.py").'
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "glob": {
                        "type": "string",
                        "description": "fnmatch-style glob over source paths. Empty = list all.",
                        "default": "",
                    },
                },
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="get_file_analysis",
            description=(
                "Return the full per-file analysis for one source file "
                "in a finished job: file metadata, symbols, imports, "
                "exports, references, literals_of_interest, AST-aware "
                "chunks, metrics, and parse status. Pass `sections` to "
                'fetch only a subset (e.g. ["symbols","imports"]) to '
                "skip the chunks payload, which is the largest part."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "path": {
                        "type": "string",
                        "description": (
                            'Source-relative path (e.g. "src/foo.py"). '
                            "Use list_job_files to discover valid paths."
                        ),
                    },
                    "sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional subset of top-level sections to "
                            "return. Valid: file, module_doc, symbols, "
                            "imports, exports, references, "
                            "literals_of_interest, chunks, metrics, "
                            "parse. Empty / omitted = all sections."
                        ),
                        "default": [],
                    },
                },
                "required": ["job_id", "path"],
            },
        ),
        types.Tool(
            name="get_log_events",
            description=(
                "Tail the job's append-only event log. Useful for "
                "monitoring progress while a job is still running, or "
                "debugging a failed run. Returns parsed events plus a "
                "next_offset cursor for incremental polling."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "from_offset": {
                        "type": "integer",
                        "description": "Byte offset into log.jsonl to resume from.",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max events to return (1..100000).",
                        "default": 1000,
                    },
                },
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


def _llm_view(snap: dict[str, Any]) -> dict[str, Any]:
    """Strip host-side paths from a job snapshot for the LLM-facing
    surface. The HTTP /admin/* endpoints still expose the full snapshot
    for ops; the MCP transport doesn't, because those paths aren't
    reachable from the model anyway and just take up tokens."""
    return {
        "job_id": snap["job_id"],
        "source": snap["source"],
        "state": snap["state"],
        "progress": snap["progress"],
        "errors_count": snap["errors_count"],
        "started_at": snap["started_at"],
        "finished_at": snap["finished_at"],
        "error": snap["error"],
    }


def _job_dir_for_mcp(job_id: str) -> Path | str:
    """Resolve the on-disk job dir for an artifact-fetch tool. Returns
    a Path on success, or a string error code suitable for `_ok`."""
    job_dir = outputs.resolve_job_dir(job_id)
    if job_dir is None:
        return "unknown_job_id"
    return job_dir


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
            job_id, _ = jobs.start_index_repo(arguments)
            return _ok({"job_id": job_id})

        if name == "get_job_status":
            snap = jobs.get_status(arguments["job_id"])
            if snap is None:
                return _ok({"error": "unknown_job_id"})
            return _ok(_llm_view(snap))

        if name == "cancel_job":
            return _ok({"ok": jobs.cancel(arguments["job_id"])})

        if name in {
            "get_manifest",
            "get_tree",
            "get_all_symbols",
            "get_top_level_symbols",
            "get_languages",
            "get_errors",
            "get_file_analysis",
            "get_analysis_index",
            "list_job_files",
            "get_log_events",
        }:
            jd = _job_dir_for_mcp(arguments["job_id"])
            if isinstance(jd, str):
                return _ok({"error": jd})
            try:
                if name == "get_manifest":
                    return _ok(outputs.read_manifest(jd))
                if name == "get_languages":
                    return _ok(outputs.read_languages(jd))
                if name == "get_errors":
                    return _ok(outputs.read_errors(jd))
                if name == "get_analysis_index":
                    return _ok(outputs.read_analysis_index(jd))
                if name == "get_tree":
                    return _ok(
                        outputs.read_tree(
                            jd,
                            path_prefix=arguments.get("path_prefix") or "",
                            analyzed_only=bool(arguments.get("analyzed_only", False)),
                        )
                    )
                if name == "get_all_symbols":
                    return _ok(
                        outputs.read_all_symbols(
                            jd,
                            prefix=arguments.get("prefix") or "",
                            kind=arguments.get("kind") or "",
                            file_prefix=arguments.get("file_prefix") or "",
                        )
                    )
                if name == "get_top_level_symbols":
                    return _ok(
                        outputs.read_top_level_symbols(
                            jd,
                            prefix=arguments.get("prefix") or "",
                            kind=arguments.get("kind") or "",
                            file_prefix=arguments.get("file_prefix") or "",
                        )
                    )
                if name == "get_file_analysis":
                    sections = arguments.get("sections") or None
                    if sections == []:
                        sections = None
                    return _ok(
                        outputs.read_file_analysis(
                            jd,
                            arguments["path"],
                            sections=sections,
                        )
                    )
                if name == "list_job_files":
                    return _ok(
                        outputs.list_file_artifacts(
                            jd,
                            glob=arguments.get("glob") or "",
                        )
                    )
                if name == "get_log_events":
                    result = jobs.read_log(
                        arguments["job_id"],
                        from_offset=max(0, int(arguments.get("from_offset", 0))),
                        limit=max(1, min(int(arguments.get("limit", 1000)), 100_000)),
                    )
                    if result is None:
                        return _ok({"error": "unknown_job_id"})
                    return _ok(result)
            except outputs.ArtifactMissing as e:
                return _ok({"error": "artifact_missing", "detail": str(e)})
            except outputs.OutputError as e:
                return _ok({"error": "bad_request", "detail": str(e)})

        if name == "list_supported_languages":
            return _ok(languages.list_supported())

        if name == "detect_language":
            lang = languages.detect(arguments["path"], first_line=arguments.get("first_line") or "")
            return _ok({"language": lang})
    except NotImplementedError as e:
        return _ok({"error": "not_implemented", "detail": str(e)})

    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# MCP prompts — workflow templates the server ships alongside its tools.


@mcp.list_prompts()
async def list_prompts() -> list[types.Prompt]:
    return prompts.list_prompts()


@mcp.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None = None) -> types.GetPromptResult:
    return prompts.get_prompt(name, arguments)


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


@router.get("/outputs/{job_id}/all_symbols.json", tags=["outputs"])
async def outputs_all_symbols(job_id: str) -> FileResponse:
    return _serve_named_json(job_id, "all_symbols.json")


@router.get("/outputs/{job_id}/top_level_symbols.json", tags=["outputs"])
async def outputs_top_level_symbols(job_id: str) -> FileResponse:
    return _serve_named_json(job_id, "top_level_symbols.json")


@router.get("/outputs/{job_id}/analysis_index.json", tags=["outputs"])
async def outputs_analysis_index(job_id: str) -> FileResponse:
    return _serve_named_json(job_id, "analysis_index.json")


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
