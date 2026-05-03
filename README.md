# parkview-codeparse-server

MCP server that performs tree-sitter-based source code analysis for the
Cobgrind LLM-Wiki daemon.

## Run

```bash
uv sync
uv run python -m parkview_codeparse
```

The server listens on `PORT` (default `35832`) with:

- `POST /sse` — MCP Streamable HTTP transport.
- `GET /health` — liveness probe.
- `GET /admin/*` — read-only JSON ops endpoints.
- `GET /docs` — OpenAPI / Swagger UI for the HTTP API.

## MCP tools

- `analyze_file(content, filename?, language?, options?)` — parse a single
  file passed inline; returns structural JSON.
- `index_repo(source, output_dir, options?)` — start a job that indexes a
  whole repo and writes per-file artifacts plus a manifest under `output_dir`.
  `source` can be a local directory, a GitHub URL
  (`https://github.com/owner/repo`), or a GitLab URL
  (`https://gitlab.com/owner/repo`, including nested groups
  `https://gitlab.com/group/sub/repo`). Pass `git_ref` to pick a specific
  branch / tag / sha. Returns `{ job_id }`.
- `get_job_status(job_id)` — poll a running or completed job.
- `cancel_job(job_id)` — cooperative cancel.
- `list_supported_languages()` — capability discovery.
- `detect_language(path)` — extension/shebang detection helper.

## Resource requirements

When `index_repo` runs against a GitHub URL, the server uses a partial
clone with bin-packed batched fetching. That gives a small, predictable
disk footprint regardless of how large the source repo is:

- **Source-side scratch space: ~100 MB peak** in `output_dir/.source/`
  during analysis. The server fetches each batch of source files
  (totaling ≤ `max_partial_clone_bytes`, default 100 MB), analyzes
  them, deletes them from the working tree, then fetches the next
  batch. Even on a multi-GB monorepo, peak local-disk used for source
  content stays at ~100 MB. Tunable via the `max_partial_clone_bytes`
  option on `index_repo`.

- **Output artifacts: roughly 1-2× the analyzed-source size.** Each
  analyzed file produces a JSON artifact under `output_dir/files/`
  containing symbols, imports, references, chunks, etc. These persist
  past the job — cobgrind reads them as it ingests the wiki — and are
  the largest *durable* footprint.

- **Memory: modest.** A `ProcessPoolExecutor` runs roughly
  `2 × CPU count` workers, each holding one file's bytes plus its
  tree-sitter parse tree in memory. Source files are capped at
  `max_file_bytes` (default 2 MB), so worst case is ~16-32 MB of
  resident source + parse trees on a typical 8-core box.

- **Network:** one provider-API pre-flight to plan the batches (GitHub
  Trees REST or GitLab REST tree + GraphQL; free for public repos, set
  `GITHUB_TOKEN` / `GITLAB_TOKEN` for higher quotas and private-repo
  access), plus one `git fetch-pack` round-trip per batch. For a
  typical sub-100 MB repo that's two HTTP hits total.

For local-path sources nothing is fetched and nothing is cloned —
the only on-disk cost is the output artifacts.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `PORT` | `35832` | HTTP listen port. |
| `HOST` | `0.0.0.0` | HTTP bind address. |
| `JOB_HISTORY_MAX` | `100` | In-memory job-table cap. |
| `DEFAULT_MAX_FILE_BYTES` | `2097152` | Default per-file size cap. |
| `DEFAULT_CHUNK_MAX_TOKENS` | `800` | Default chunk size for cAST chunking. |
| `GITHUB_TOKEN` | unset | Optional, raises GitHub Trees API quota from 60 to 5000 req/hr and unlocks private repos. |
| `GITLAB_TOKEN` | unset | Optional, used for GitLab API auth and private-repo access. |

See `/Users/gary/.claude/plans/cobgrind-is-a-daemon-fluttering-sutton.md` for
the full design.
