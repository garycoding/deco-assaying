# deco-assaying

MCP server that performs tree-sitter-based source code analysis. Designed
to feed structural information about a repo (symbols, imports, references,
chunks, metrics) into a downstream consumer that maintains a knowledge
base over many codebases.

## Run

Five ways to run it. Pick whichever matches your situation.

| Mode | When to use |
|---|---|
| [1. uvx (one-off)](#1-one-off--uvx) | Try it once, no install. |
| [2. uv tool install (pinned daemon)](#2-pinned-daemon--uv-tool-install) | Run it occasionally, want it on `$PATH`. |
| [3. macOS LaunchAgent](#3-macos-persistent-daemon-launchd) | Persistent daemon on a Mac. |
| [4. Linux systemd user unit](#4-linux-persistent-daemon-systemd) | Persistent daemon on Linux. |
| [5. Docker / docker compose](#5-docker--ghcr) | Container deployment, ops stack, shared host. |

### Prereqs

- **uv-based modes (1–4)** need [`uv`](https://docs.astral.sh/uv/) and `git`.
  uv ships a portable Python 3.13, so no system Python install required.

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

- **Docker mode (5)** needs `docker` (or compatible). The image bundles
  Python 3.13 and git; nothing else on the host.

In every mode the server listens on `PORT` (default `35832`). Sanity-check
it's up:

```bash
curl http://127.0.0.1:35832/health
```

---

### 1. One-off — `uvx`

`uvx` resolves the package into a temporary venv and runs it once.
Nothing persists between runs.

```bash
uvx deco-assaying                       # latest release
uvx deco-assaying@0.1.5                 # pin a specific version

# With env vars (e.g. private-repo token, custom output dir):
PUBLIC_BASE_URL=http://localhost:35832 \
GITHUB_TOKEN=ghp_xxx \
OUTPUT_ROOT=$HOME/da-output \
  uvx deco-assaying
```

Good for kicking the tires or running on a CI box where you don't want
to leave anything on disk.

### 2. Pinned daemon — `uv tool install`

Installs the `deco-assaying` command on your `$PATH`, isolated in its
own venv that uv manages. Faster startup than `uvx` (no resolve on
each run).

```bash
uv tool install deco-assaying
deco-assaying                           # foreground server
```

To upgrade later: `uv tool upgrade deco-assaying`.
To remove: `uv tool uninstall deco-assaying`.

To run in the background as your user (no init system):

```bash
nohup deco-assaying > ~/da.log 2>&1 &
```

For a real "always running" setup, see the launchd / systemd recipes
below.

### 3. macOS persistent daemon (launchd)

After `uv tool install deco-assaying`, register a LaunchAgent so the
daemon starts at login and restarts if it crashes.

Save this as `~/Library/LaunchAgents/com.garycoding.deco-assaying.plist`
(replace `CHANGE-ME` with your username):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.garycoding.deco-assaying</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/CHANGE-ME/.local/bin/deco-assaying</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>OUTPUT_ROOT</key>
    <string>/Users/CHANGE-ME/deco-assaying-output</string>
    <key>PUBLIC_BASE_URL</key>
    <string>http://localhost:35832</string>
    <!-- Uncomment for private-repo access:
    <key>GITHUB_TOKEN</key>
    <string>ghp_xxx</string>
    -->
  </dict>

  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>

  <key>StandardOutPath</key>
  <string>/Users/CHANGE-ME/Library/Logs/deco-assaying.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/CHANGE-ME/Library/Logs/deco-assaying.err.log</string>
</dict>
</plist>
```

Load and start it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.garycoding.deco-assaying.plist
launchctl kickstart  -k gui/$(id -u)/com.garycoding.deco-assaying

# Check status:
launchctl print gui/$(id -u)/com.garycoding.deco-assaying | head -30

# Tail logs:
tail -f ~/Library/Logs/deco-assaying.{out,err}.log

# Stop / unload:
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.garycoding.deco-assaying.plist
```

### 4. Linux persistent daemon (systemd)

After `uv tool install deco-assaying`, register a user-scope systemd
unit so no root is required.

Save this as `~/.config/systemd/user/deco-assaying.service`:

```ini
[Unit]
Description=deco-assaying MCP server
After=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/deco-assaying
Restart=on-failure
RestartSec=5
Environment=OUTPUT_ROOT=%h/deco-assaying-output
Environment=PUBLIC_BASE_URL=http://localhost:35832
# Uncomment for private-repo access:
# Environment=GITHUB_TOKEN=ghp_xxx
# Environment=GITLAB_TOKEN=glpat-xxx

[Install]
WantedBy=default.target
```

Enable and start:

```bash
systemctl --user daemon-reload
systemctl --user enable --now deco-assaying

# Check status:
systemctl --user status deco-assaying

# Tail logs:
journalctl --user -u deco-assaying -f

# Stop:
systemctl --user disable --now deco-assaying
```

To keep the daemon running when the user is logged out, enable lingering:

```bash
loginctl enable-linger "$USER"
```

### 5. Docker / GHCR

Pull the published multi-arch image (linux/amd64 + linux/arm64) and
run it directly:

```bash
docker pull ghcr.io/garycoding/deco-assaying:latest

docker run --rm \
  -p 35832:35832 \
  -e PUBLIC_BASE_URL=http://localhost:35832 \
  -v deco-assaying-data:/data \
  ghcr.io/garycoding/deco-assaying:latest
```

Pin a specific version with a tag — `:0.1.5`, `:0.1`, or `:latest`. See
the [container registry](https://github.com/garycoding/deco-assaying/pkgs/container/deco-assaying)
for available tags.

For a real deployment, copy [`docker-compose.yml`](docker-compose.yml),
edit the `CHANGE-ME` placeholders (most importantly `PUBLIC_BASE_URL`),
then:

```bash
docker compose up -d                    # start in background
docker compose logs -f                  # tail logs
docker compose pull && docker compose up -d   # upgrade
docker compose down                     # stop, keep volume
docker compose down -v                  # stop and drop the volume
```

The compose file shows two volume options: a docker-managed named
volume (default) or a bind mount to a host path you can browse
directly. Switch by commenting / uncommenting the relevant lines.

For a [Portainer](https://www.portainer.io/) stack, paste the contents
of `docker-compose.yml` into the stack editor (lowercase stack name —
Portainer rejects caps).

### From source (for development)

```bash
git clone https://github.com/garycoding/deco-assaying.git
cd deco-assaying
uv sync
uv run python -m deco_assaying
```

## Endpoints

- `POST /sse` — MCP Streamable HTTP transport. Tools, prompts.
- `GET /health` — liveness probe.
- `GET /admin/*` — read-only JSON ops endpoints.
- `GET /outputs/{job_id}/...` — read-only download API for job artifacts.
- `GET /docs` — OpenAPI / Swagger UI for the HTTP API.

HTTP responses are gzipped when the client sends `Accept-Encoding: gzip`
(transparent for any modern client).

## MCP tools

The MCP server exposes a small surface; descriptions in the tools'
schemas explain the recommended order of use. Highlights:

- `analyze_file(content, filename?, language?, ...)` — analyze ONE file
  whose source you already have, inline. Don't use this to analyze a
  whole repo.
- `index_repo(source, options?)` — start an async indexing job. Returns
  `{job_id}`. `source` can be a local directory, a GitHub URL, or a
  GitLab URL (including nested groups).
- `get_job_status(job_id)` — poll until `state == "done"`.
- `cancel_job(job_id)` — cooperative cancel.
- `get_manifest(job_id)` — repo-level summary. Read first.
- `get_analysis_index(job_id)` — sizes + absolute download URLs for
  every analysis file. Read second to plan which artifacts to fetch
  and which (if any) to side-route via a fetch tool to avoid
  context-window overflow.
- `get_top_level_symbols(job_id, ...)` — module-level definitions only.
  The cheap default for understanding repo shape.
- `get_all_symbols(job_id, ...)` — every definition (including methods
  + nested classes). Heavier; use for cross-cutting "find every X"
  queries.
- `get_tree(job_id, path_prefix?, analyzed_only?)` — full path inventory
  with size totals.
- `get_languages(job_id)` / `get_errors(job_id)` — small rollups.
- `list_job_files(job_id, glob?)` — paths of every per-file artifact.
- `get_file_analysis(job_id, path, sections?)` — drill into one file's
  analysis. Pass `sections=` to skip the chunks payload.
- `get_log_events(job_id, ...)` — tail the run log (during or after).
- `list_supported_languages()` / `detect_language(path)` — capability
  helpers.

## MCP prompts (workflow templates)

The server ships two prompts so any client picking it up inherits the
recommended workflow without reading this README:

- `analyze_repo(source, focus?)` — full lifecycle from scratch.
- `explore_finished_job(job_id, question)` — focused question against
  a finished job.

Both explain the URL-fallback strategy: when an artifact is too big
for the context window, an agent paired with a generic HTTP fetch
tool can hand its absolute `url` (from `analysis_index.json`) to
out-of-band processing instead of inlining the raw content.

## Output download API

Every job's artifacts land under `OUTPUT_ROOT/{job_id}/`. A consumer
sharing the volume can read them off disk; one without a shared volume
pulls them over HTTP:

| Endpoint | Returns |
|---|---|
| `GET /outputs/{job_id}` | `manifest.json` (convenience). |
| `GET /outputs/{job_id}/manifest.json` | Repo-level rollup. |
| `GET /outputs/{job_id}/analysis_index.json` | Sizes + absolute URLs for every artifact. |
| `GET /outputs/{job_id}/tree.json` | Full path inventory (analyzed + skipped). |
| `GET /outputs/{job_id}/all_symbols.json` | Every definition. |
| `GET /outputs/{job_id}/top_level_symbols.json` | Module-level definitions only. |
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
| `PUBLIC_BASE_URL` | `http://localhost:${PORT}` ¹ | `http://localhost:${PORT}` ¹ | Externally-reachable URL of this daemon, used to build absolute download URLs in `analysis_index.json`. Override in any deployment where clients reach the server at a different address. |
| `JOB_HISTORY_MAX` | `100` | `100` | In-memory job-table cap. |
| `DEFAULT_MAX_FILE_BYTES` | `2097152` | `2097152` | Default per-file size cap. |
| `DEFAULT_CHUNK_MAX_TOKENS` | `800` | `800` | Default chunk size for cAST chunking. |
| `GITHUB_TOKEN` | unset | unset | Optional. Raises GitHub Trees API quota from 60 to 5000 req/hr and unlocks private repos. |
| `GITLAB_TOKEN` | unset | unset | Optional. GitLab API auth + private-repo access. |

¹ The `PUBLIC_BASE_URL` default is built from `PORT` at startup. If you set `PORT=8080` without setting `PUBLIC_BASE_URL`, the default becomes `http://localhost:8080`.

## Releasing

Tag-driven via the `Release` workflow on push of a `v*` tag. Use the [`ParkviewLab/dev-tools`](https://github.com/ParkviewLab/dev-tools) helpers — they enforce the SSOT-tag-CI loop (`pyproject.toml` is the only place the version lives; the workflow's `Verify tag matches pyproject version` step would fail otherwise).

```sh
git bump patch              # 0.1.5 → 0.1.6, committed
git release                 # annotated tag v0.1.6 from pyproject.toml
git push --follow-tags      # CI fires
```

The workflow builds a multi-arch image (linux/amd64 + linux/arm64) and pushes it to GHCR with `vX.Y.Z`, `vX.Y`, and `latest` tags, in parallel with publishing wheel + sdist to PyPI via trusted publishing. ~3-5 minutes end-to-end.

Don't have the helpers? Install once: `git clone https://github.com/ParkviewLab/dev-tools.git ~/dev-tools && cd ~/dev-tools && ./install.sh`.
