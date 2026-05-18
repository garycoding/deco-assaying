"""Language detection and grammar loading.

Detection is filesystem-free: we look at the filename extension, optionally
the first line for shebangs. Grammar loading delegates to
`tree-sitter-language-pack`, which ships pre-compiled grammars and `tags.scm`
queries for ~165 languages.
"""

from __future__ import annotations

import os
from functools import cache
from typing import Any

from deco_assaying.config import FULL_SUPPORT_LANGUAGES

# Map of file extension (without leading dot, lowercase) -> language id used
# by tree-sitter-language-pack. Only common extensions are listed; languages
# not here can still be requested explicitly via the `language` argument.
_EXTENSIONS: dict[str, str] = {
    "py": "python",
    "pyi": "python",
    "ts": "typescript",
    "tsx": "tsx",
    "js": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "jsx": "javascript",
    "go": "go",
    "rs": "rust",
    "java": "java",
    "rb": "ruby",
    "c": "c",
    "h": "c",
    "cpp": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "hpp": "cpp",
    "hh": "cpp",
    "hxx": "cpp",
    "cs": "csharp",
    "php": "php",
    "sh": "bash",
    "bash": "bash",
    "kt": "kotlin",
    "kts": "kotlin",
    "swift": "swift",
    "scala": "scala",
    "lua": "lua",
    "r": "r",
    "pl": "perl",
    "pm": "perl",
    "ex": "elixir",
    "exs": "elixir",
    "erl": "erlang",
    "hrl": "erlang",
    "hs": "haskell",
    "ml": "ocaml",
    "mli": "ocaml",
    "clj": "clojure",
    "cljs": "clojure",
    "dart": "dart",
    "zig": "zig",
    "nim": "nim",
    "jl": "julia",
    "sql": "sql",
    "html": "html",
    "htm": "html",
    "css": "css",
    "scss": "scss",
    "vue": "vue",
    "svelte": "svelte",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
    "json": "json",
    "md": "markdown",
    "markdown": "markdown",
    "dockerfile": "dockerfile",
    "make": "make",
    "cmake": "cmake",
    "proto": "proto",
    "graphql": "graphql",
    "gql": "graphql",
    "tf": "hcl",
    "groovy": "groovy",
}

_SHEBANGS: dict[str, str] = {
    "python": "python",
    "python3": "python",
    "node": "javascript",
    "bash": "bash",
    "sh": "bash",
    "zsh": "bash",
    "ruby": "ruby",
    "perl": "perl",
    "php": "php",
}

_DISPLAY_NAMES: dict[str, str] = {
    "python": "Python",
    "typescript": "TypeScript",
    "tsx": "TSX",
    "javascript": "JavaScript",
    "go": "Go",
    "rust": "Rust",
    "java": "Java",
    "ruby": "Ruby",
    "c": "C",
    "cpp": "C++",
    "csharp": "C#",
    "php": "PHP",
    "bash": "Bash",
}


def detect(path: str, first_line: str = "") -> str:
    """Return a language id, or "" if unknown."""
    name = os.path.basename(path).lower()
    if name in {"dockerfile"}:
        return "dockerfile"
    if name in {"makefile", "gnumakefile"}:
        return "make"
    if name in {"cmakelists.txt"}:
        return "cmake"

    if "." in name:
        ext = name.rsplit(".", 1)[1]
        if ext in _EXTENSIONS:
            return _EXTENSIONS[ext]

    if first_line.startswith("#!"):
        line = first_line[2:].strip()
        if line.startswith("/usr/bin/env "):
            line = line[len("/usr/bin/env ") :].strip()
        prog = line.split()[0] if line else ""
        prog = os.path.basename(prog).lower()
        if prog in _SHEBANGS:
            return _SHEBANGS[prog]

    return ""


def list_supported() -> list[dict[str, Any]]:
    """All languages we know about, with full-support flag.

    `has_full_support=True` means analyze.py emits the rich shape (symbols,
    imports, references, metrics). Otherwise we still produce file metadata,
    module_doc, and chunks but skip the language-specific extractors.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    sources: list[str] = list(_EXTENSIONS.values()) + list(_SHEBANGS.values()) + list(FULL_SUPPORT_LANGUAGES)
    for lang in sources:
        if lang in seen:
            continue
        seen.add(lang)
        out.append(
            {
                "id": lang,
                "display_name": _DISPLAY_NAMES.get(lang, lang.replace("_", " ").title()),
                "has_full_support": lang in FULL_SUPPORT_LANGUAGES,
            }
        )
    out.sort(key=lambda x: x["id"])
    return out


# Languages whose tree-sitter grammar is GPL-licensed. We never load these so
# tree-sitter-language-pack doesn't fetch the GPL grammar into the local cache
# (which would create a GPL artifact alongside this MIT-licensed server).
# Currently only `ebnf` (RubixDev/ebnf is GPL-3.0).
_BLOCKED_LANGUAGES: frozenset[str] = frozenset({"ebnf"})


@cache
def get_parser(language: str):
    """Return a tree-sitter Parser for the given language id, or None.

    Cached per-process so the parser/grammar are reused across calls.
    """
    if language in _BLOCKED_LANGUAGES:
        return None
    try:
        from tree_sitter_language_pack import get_parser as _gp  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        # tree-sitter-language-pack types this as a Literal of supported ids;
        # we only know the language id at runtime, so we pass through as Any.
        return _gp(language)  # ty: ignore[invalid-argument-type]
    except Exception:
        return None


@cache
def get_language(language: str):
    """Return a tree-sitter Language for the given language id, or None."""
    if language in _BLOCKED_LANGUAGES:
        return None
    try:
        from tree_sitter_language_pack import get_language as _gl  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        return _gl(language)  # ty: ignore[invalid-argument-type]
    except Exception:
        return None
