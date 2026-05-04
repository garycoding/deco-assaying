# deco-assaying — architecture

A 30-minute orientation for someone who's about to read or change the
code. Not a spec; the source is the spec.

## What it does

Given a repo (local path, GitHub URL, or GitLab URL), produce a
structured analysis of every source file:

- per-file JSON with symbols, imports, exports, references, literals,
  metrics, and AST-aware chunks
- repo-level rollups: a manifest, a global symbol index, language
  counts, parse errors, and a complete path inventory (`tree.json`)

Two surfaces:

- **MCP tools** at `POST /sse` — `analyze_file` (inline content) and
  `index_repo` (whole-repo job).
- **HTTP** for ops + artifact retrieval — `/health`, `/admin/*`,
  `/outputs/{job_id}/...`.

## Process model

One Python process. Three concurrency layers:

| Layer | Purpose |
|---|---|
| asyncio (FastAPI/uvicorn) | HTTP request handling and the MCP transport. |
| One thread per indexing job | Owns the job's filesystem outputs and the `log.jsonl` writer. Spawned by `jobs.start_index_repo`. |
| `ProcessPoolExecutor` per job | Per-file analysis. Spawned (not forked) so tree-sitter native state is clean per worker. Worker count = CPU count. |

The job table (`jobs._jobs`) is an `OrderedDict` guarded by
`threading.Lock`. Bounded at `JOB_HISTORY_MAX`; only terminal jobs are
evicted, so an active job is never silently dropped.

## Module map

### Core orchestration

- **`__main__`** — `python -m deco_assaying` entry point. Configures
  logging, then runs uvicorn against `app.app`.
- **`app`** — FastAPI app construction, CORS, MCP `/sse` mount.
- **`routes`** — `APIRouter` + the MCP `Server` + every tool handler.
  Lifespan starts the MCP session manager and the retention sweeper.
- **`config`** — env-var-driven constants. Pure leaf module.
- **`jobs`** — the job table and `_run_job` orchestrator. Pick a fetch
  strategy (local / single-clone / streaming), walk, dispatch to the
  pool, drain completions, write the rollups.

### Source resolution

- **`source`** — validates `source` arguments, allocates
  `OUTPUT_ROOT/{job_id}/`, runs `git clone`, refuses unsafe URLs.
- **`providers`** — dispatcher: maps a URL to the right provider
  module by host.
- **`github`** — GitHub-specific helpers: `parse_url`,
  `fetch_default_branch`, `fetch_blob_sizes` (Trees API), and
  `fetch_blob_via_raw` (used by the streaming path).
- **`gitlab`** — same shape for gitlab.com (REST tree + GraphQL for
  sizes; raw blob URLs).
- **`walker`** — directory walk with `.gitignore` + binary/size
  filters. Two entry points: `walk_full` (on-disk) and
  `walk_from_inventory` (driven by a provider's blob list).

### Analysis

- **`analyze`** — `analyze_inline(content, filename, language?, ...)`.
  Detects the language, picks an analyzer, invokes tree-sitter, runs
  the analyzer, and packages the result.
- **`languages`** — extension + shebang detection. Cached grammar
  loader (`get_parser_for_language`).
- **`analyzers/*`** — per-language analyzers. All conform to a single
  `analyze(node, source_text)` signature returning the documented
  per-file shape.
- **`chunks`** — cAST-style AST-aware chunking. Splits on syntactic
  boundaries; tags each chunk with the qualified name of its enclosing
  symbol.
- **`literals`** — extracts URLs, paths, env-var lookups, SQL, and
  route strings from string-literal nodes.
- **`detectors`** — `is_test`, `is_generated`, `is_config` heuristics.
- **`manifest`** — repo-level rollup writers.

### Phase 2 surfaces

- **`outputs`** — helpers for the download API: path-traversal-safe
  resolution, directory listing, glob expansion, streaming-ZIP
  generator, and a `JobDirRow` listing for `/admin/outputs`.
- **`retention`** — `sweep_once()` plus the `run_forever()` lifespan
  task that purges dirs older than `OUTPUT_EXPIRY_DAYS`.

## Data flow — `index_repo`

```
MCP `index_repo`
    │
    ▼
jobs.start_index_repo(args)
    │  allocates OUTPUT_ROOT/{job_id}/
    │  registers in the job table (status=pending)
    │  spawns a thread → _run_job
    └─► returns (job_id, output_path)

_run_job (background thread)
    │
    │ source resolution
    │   ├─ local path        → walker.walk_full
    │   ├─ provider URL +
    │   │   provider API +
    │   │   planned > cap    → STREAMING (no clone, batched raw fetch)
    │   └─ otherwise         → SINGLE PARTIAL CLONE
    │
    │ for each batch / pass:
    │   ProcessPoolExecutor.submit(_worker_disk, ...)
    │       (worker reads file, runs analyze.analyze_inline,
    │        returns the per-file result)
    │   main thread:
    │     atomic-write files/{rel}.json
    │     append log.jsonl event
    │     update progress counters
    │
    │ on finish: manifest.write(...) → manifest.json + rollups
    └─► status=done
```

The streaming path bin-packs files into batches ≤
`max_partial_clone_bytes`, fetches each batch via the provider's raw
URL, analyzes in parallel, then **deletes the batch's files** before
fetching the next. Peak source-side disk = one batch's worth.

## Output layout

Every job lands at `OUTPUT_ROOT/{job_id}/`:

```
{job_id}/
  manifest.json        # written last; signals completion
  tree.json            # every path the walker observed (analyzed + skipped)
  symbols.json         # global qualified_name → (file, span) index
  languages.json       # per-language file counts + bytes
  errors.json          # parse errors + skipped files
  log.jsonl            # append-only event stream
  files/               # mirrors source layout; one .json per analyzed file
    src/foo.py.json
    src/bar/baz.ts.json
  .source/             # cloned repo (absent for local-source jobs;
                       # streaming mode keeps it as scratch and ends empty)
```

All file writes are atomic (`.tmp` then rename). `manifest.json` lands
last so a file-watching consumer can use its existence as the "done"
signal without polling.

## HTTP surface

| Path | Purpose |
|---|---|
| `POST /sse` | MCP Streamable HTTP transport. Stateless. |
| `GET /health` | Liveness. |
| `GET /admin/version` | Process + key-dependency versions. |
| `GET /admin/languages` | Grammar capability matrix. |
| `GET /admin/jobs[/{id}[/log]]` | Job table + log tail. |
| `GET /admin/stats` | Process-level counters. |
| `GET /admin/outputs` | Job dirs on disk under `OUTPUT_ROOT`. |
| `GET /outputs/{id}` | Convenience: serves `manifest.json`. |
| `GET /outputs/{id}/{rollup}.json` | Direct rollups. |
| `GET /outputs/{id}/log.jsonl` | Tail. |
| `GET /outputs/{id}/ls` | Listing. |
| `GET /outputs/{id}/file/{path}` | Single file, or streaming ZIP if any segment globs. |
| `GET /outputs/{id}/zip` | Explicit-bulk-zip alias. |
| `DELETE /outputs/{id}` | Remove dir + drop table row. 409 if active. |
| `GET /docs`, `/redoc`, `/openapi.json` | Auto-generated schema. |

## Security boundaries

- **URL validation** — `source.is_repo_url` + provider-specific
  `parse_url` reject anything other than canonical `https://github.com`
  or `https://gitlab.com` URLs (no SSH, no `file://`, no IP literals).
- **Path traversal** — every consumer-supplied path under
  `/outputs/{id}/...` runs through `outputs.safe_subpath`, which
  resolves against the job dir and rejects anything that escapes
  (covers symlinks, `..`, absolute paths).
- **Active-job protection** — `jobs.is_active` gates both
  `DELETE /outputs/{id}` (returns 409) and the retention sweeper
  (skips active jobs).
- **No auth in v2** — relies on network isolation. Bearer-token
  middleware is straightforward to add later.

## Configuration

See the env-var table in [README.md](../README.md#configuration). Two
notable knobs:

- `OUTPUT_ROOT` is the **only** place jobs write. The MCP tool no
  longer accepts an output path; the server allocates `{job_id}/`.
- `OUTPUT_EXPIRY_DAYS=0` disables the retention sweeper for ops who
  want manual control via `DELETE /outputs/{id}`.

## Deployment

Two equivalent shapes:

1. **Daemon** — `uv tool install deco-assaying` or `uv run python -m
   deco_assaying`. `OUTPUT_ROOT=./output`. Same machine as the
   consumer; the consumer reads artifacts off disk.
2. **Container** — `ghcr.io/garycoding/deco-assaying`.
   `OUTPUT_ROOT=/data` on a named volume. Consumer either shares the
   volume or pulls artifacts via the download API.

The server is identical in both cases; only `OUTPUT_ROOT` differs.

## Out of scope (today)

- Auth on any endpoint.
- Self-hosted GitHub Enterprise / GitLab CE.
- Incremental re-index (file-level sha-based skip).
- Cross-file resolution (the consumer's job).
- CPU/memory quotas (use container-level limits).
