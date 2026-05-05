"""Static configuration. Pure leaf module — no internal imports."""

import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    VERSION: str = version("deco-assaying")
except PackageNotFoundError:  # editable install before first build
    VERSION = "0.0.0+local"

PORT = int(os.environ.get("PORT", "35832"))
HOST = os.environ.get("HOST", "0.0.0.0")

# Externally-reachable base URL of this daemon. Used to build absolute
# download URLs in `analysis_index.json` so consumers can fetch
# artifacts (or hand them to a separate fetch tool) without the
# daemon having to know how it's being reached.
#
# Default is good for local dev. In container/server deployments,
# set this to the URL clients actually use:
#   PUBLIC_BASE_URL=http://da.lan:35832
#   PUBLIC_BASE_URL=https://deco.example.com
#
# Misconfiguration won't break the daemon — the URLs in the index will
# just point at the wrong place; consumers can recover by composing
# URLs themselves from the `outputs_path` field.
PUBLIC_BASE_URL: str = (os.environ.get("PUBLIC_BASE_URL") or f"http://localhost:{PORT}").rstrip("/")

# Where the server allocates per-job output directories. Each job gets
# `OUTPUT_ROOT/{job_id}/` with the manifest, log, files/, etc. The
# server always picks the path itself; callers never supply one. In
# the daemon deployment the default `./output` (relative to CWD) is
# fine; in the Dockerfile we override `OUTPUT_ROOT=/data` and mount a
# named volume there.
OUTPUT_ROOT: Path = Path(os.environ.get("OUTPUT_ROOT", "./output")).resolve()

# How many days a finished job's output dir lingers before the
# retention sweeper purges it. `0` disables the sweeper entirely.
OUTPUT_EXPIRY_DAYS: int = int(os.environ.get("OUTPUT_EXPIRY_DAYS", "7"))

# Job table bounds (in-memory only in v1).
JOB_HISTORY_MAX = int(os.environ.get("JOB_HISTORY_MAX", "100"))

# Per-file analyzer defaults; overridable per call.
DEFAULT_MAX_FILE_BYTES = int(os.environ.get("DEFAULT_MAX_FILE_BYTES", str(2 * 1024 * 1024)))
DEFAULT_CHUNK_MAX_TOKENS = int(os.environ.get("DEFAULT_CHUNK_MAX_TOKENS", "800"))

# Peak source-side scratch space during a GitHub-clone job. When the
# Trees API tells us the planned source download exceeds this number,
# the job switches from a single partial clone to a bin-packed
# streaming fetch — files are pulled in batches of <= this many bytes,
# analyzed, then deleted from disk before the next batch arrives.
DEFAULT_MAX_PARTIAL_CLONE_BYTES = int(
    os.environ.get("DEFAULT_MAX_PARTIAL_CLONE_BYTES", str(100 * 1024 * 1024))
)

# Languages with hand-tuned support in v1. Others fall back to a minimal
# extractor (see analyze.py).
FULL_SUPPORT_LANGUAGES: tuple[str, ...] = (
    "python",
    "typescript",
    "tsx",
    "javascript",
    "go",
    "rust",
    "java",
    "ruby",
    "c",
    "cpp",
    "csharp",
    "php",
    "bash",
)
