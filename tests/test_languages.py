from deco_assaying import languages


def test_extension_detection():
    assert languages.detect("foo.py") == "python"
    assert languages.detect("a/b/c.ts") == "typescript"
    assert languages.detect("a/b/c.tsx") == "tsx"
    assert languages.detect("Foo.JAVA") == "java"
    assert languages.detect("script.sh") == "bash"


def test_special_basenames():
    assert languages.detect("Dockerfile") == "dockerfile"
    assert languages.detect("path/to/Makefile") == "make"
    assert languages.detect("CMakeLists.txt") == "cmake"


def test_shebang_detection():
    assert languages.detect("noext", first_line="#!/usr/bin/env python3") == "python"
    assert languages.detect("noext", first_line="#!/bin/bash") == "bash"
    assert languages.detect("noext", first_line="#!/usr/bin/env node") == "javascript"


def test_unknown():
    assert languages.detect("foo.xyz") == ""
    assert languages.detect("") == ""


def test_list_supported_includes_full_support_languages():
    listed = {row["id"] for row in languages.list_supported()}
    for required in ("python", "typescript", "javascript", "go", "rust", "java", "bash"):
        assert required in listed
    fully = [row for row in languages.list_supported() if row["has_full_support"]]
    assert {row["id"] for row in fully} >= {"python", "typescript", "javascript"}


def test_ebnf_grammar_is_blocked():
    # ebnf is GPL-3.0 (RubixDev/ebnf) — must never be loaded by this MIT server.
    assert languages.get_parser("ebnf") is None
    assert languages.get_language("ebnf") is None
