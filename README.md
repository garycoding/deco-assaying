# deco-assaying

MCP server that performs tree-sitter-based source code analysis. Designed
to feed structural information about a repo (symbols, imports, references,
chunks, metrics) into a downstream consumer that maintains a knowledge
base over many codebases.

## Run

Three options, ordered by ease-of-install for the consumer.

### 1. `uv tool install` (PyPI)

```bash
uv tool install deco-assaying
deco-assaying
```

You need [`uv`](https://docs.astral.sh/uv/) and `git`. uv ships its own
portable Python 3.13, so no system Python install required.

### 2. Docker / GHCR

```bash
docker run --rm -p 35832:35832 -v deco-assaying-data:/data \
  ghcr.io/garycoding/deco-assaying:latest
```

Or with compose (see [docker-compose.yml](docker-compose.yml)):

```bash
docker compose up
```

The image bundles git + Python; the named volume persists job
outputs across restarts.

### 3. From source

```bash
uv sync
uv run python -m deco_assaying
```

The server listens on `PORT` (default `35832`) with:

- `POST /sse` — MCP Streamable HTTP transport.
- `GET /health` — liveness probe.
- `GET /admin/*` — read-only JSON ops endpoints.
- `GET /outputs/{job_id}/...` — read-only download API for job artifacts.
- `GET /docs` — OpenAPI / Swagger UI for the HTTP API.

## MCP tools

- `analyze_file(content, filename?, language?, options?)` — parse a single
  file passed inline; returns structural JSON.
- `index_repo(source, options?)` — start a job that indexes a whole repo
  and writes per-file artifacts plus a manifest. The server allocates a
  fresh output dir under `OUTPUT_ROOT` and returns `{ job_id, output_path }`.
  `source` can be a local directory, a GitHub URL
  (`https://github.com/owner/repo`), or a GitLab URL
  (`https://gitlab.com/owner/repo`, including nested groups
  `https://gitlab.com/group/sub/repo`). Pass `git_ref` to pick a specific
  branch / tag / sha.
- `get_job_status(job_id)` — poll a running or completed job.
- `cancel_job(job_id)` — cooperative cancel.
- `list_supported_languages()` — capability discovery.
- `detect_language(path)` — extension/shebang detection helper.

## Output download API

Every job's artifacts land under `OUTPUT_ROOT/{job_id}/`. A consumer
sharing the volume can read them off disk; one without a shared volume
can pull them over HTTP:

| Endpoint | Returns |
|---|---|
| `GET /outputs/{job_id}` | `manifest.json` (convenience). |
| `GET /outputs/{job_id}/manifest.json` | Repo-level rollup. |
| `GET /outputs/{job_id}/tree.json` | Full path inventory (analyzed + skipped). |
| `GET /outputs/{job_id}/symbols.json` | Global qualified-name index. |
| `GET /outputs/{job_id}/languages.json` | Per-language counts. |
| `GET /outputs/{job_id}/errors.json` | Parse errors + skipped files. |
| `GET /outputs/{job_id}/log.jsonl?from_offset=N` | Tail the job's log. |
| `GET /outputs/{job_id}/ls?path=&recursive=` | Directory listing. |
| `GET /outputs/{job_id}/file/{path}` | Single file, **or** a streaming ZIP if any path segment contains `*?[`. E.g. `/file/files/**/*.py.json`. |
| `GET /outputs/{job_id}/zip?path=&match=` | Explicit-bulk-zip alias. Default = whole job dir. |
| `DELETE /outputs/{job_id}` | Remove the dir + drop the table entry. 409 if still running. |
| `GET /admin/outputs` | List every job_id present on disk under `OUTPUT_ROOT`. |

Path traversal (`..`, absolute paths, escape via symlink) is rejected.

## Resource requirements

When `index_repo` runs against a GitHub URL, the server uses a partial
clone with bin-packed batched fetching. That gives a small, predictable
disk footprint regardless of how large the source repo is:

- **Source-side scratch space: ~100 MB peak** in `output_path/.source/`
  during analysis. The server fetches each batch of source files
  (totaling ≤ `max_partial_clone_bytes`, default 100 MB), analyzes
  them, deletes them from the working tree, then fetches the next
  batch. Even on a multi-GB monorepo, peak local-disk used for source
  content stays at ~100 MB. Tunable via the `max_partial_clone_bytes`
  option on `index_repo`.

- **Output artifacts: roughly 1-2× the analyzed-source size.** Each
  analyzed file produces a JSON artifact under `output_path/files/`
  containing symbols, imports, references, chunks, etc. These persist
  past the job — the consumer reads them incrementally — and are
  the largest *durable* footprint. The retention sweeper auto-purges
  job dirs older than `OUTPUT_EXPIRY_DAYS`.

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

| Env var | Default (daemon) | Default (container) | Purpose |
|---|---|---|---|
| `PORT` | `35832` | `35832` | HTTP listen port. |
| `HOST` | `0.0.0.0` | `0.0.0.0` | HTTP bind address. |
| `OUTPUT_ROOT` | `./output` | `/data` | Where the server writes job dirs. |
| `OUTPUT_EXPIRY_DAYS` | `7` | `7` | Auto-purge job dirs older than this. `0` disables. |
| `JOB_HISTORY_MAX` | `100` | `100` | In-memory job-table cap. |
| `DEFAULT_MAX_FILE_BYTES` | `2097152` | `2097152` | Default per-file size cap. |
| `DEFAULT_CHUNK_MAX_TOKENS` | `800` | `800` | Default chunk size for cAST chunking. |
| `GITHUB_TOKEN` | unset | unset | Optional, raises GitHub Trees API quota from 60 to 5000 req/hr and unlocks private repos. |
| `GITLAB_TOKEN` | unset | unset | Optional, used for GitLab API auth and private-repo access. |

## Releasing

Tag-driven. Bump `version` in `pyproject.toml`, then:

```bash
git tag vX.Y.Z && git push --tags
```

The `Release` workflow builds a multi-arch image (linux/amd64 +
linux/arm64) and pushes it to GHCR with `vX.Y.Z`, `vX.Y`, and `latest`
tags, in parallel with publishing wheel + sdist to PyPI via trusted
publishing. ~3-5 minutes end-to-end.
