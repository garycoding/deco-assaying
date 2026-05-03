"""Static configuration. Pure leaf module — no internal imports."""

import os
from importlib.metadata import PackageNotFoundError, version

try:
    VERSION: str = version("parkview-codeparse-server")
except PackageNotFoundError:  # editable install before first build
    VERSION = "0.0.0+local"

PORT = int(os.environ.get("PORT", "35832"))
HOST = os.environ.get("HOST", "0.0.0.0")

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
