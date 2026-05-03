"""Fallback analyzer for languages without a hand-written extractor.

We still parse with tree-sitter, so chunks and a parse-status report are
honest, but symbols/imports/references stay empty. The plan promises this
behavior:

> Others remain enumerable through `list_supported_languages()` but use a
> fallback that emits only `file` + `module_doc` + `chunks` until we add
> language-specific tweaks.
"""

from __future__ import annotations

from typing import Any

from parkview_codeparse.analyzers._base import empty_result


def analyze(source_bytes: bytes, root: Any) -> dict[str, Any]:
    return empty_result()
