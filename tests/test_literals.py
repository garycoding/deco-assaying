from parkview_codeparse import literals

_SPAN = {"start_byte": 0, "end_byte": 0, "start_line": 1, "end_line": 1}


def kinds(items):
    return sorted({i["kind"] for i in items})


def values(items, kind):
    return sorted(i["value"] for i in items if i["kind"] == kind)


def test_url_extraction_with_trailing_punctuation():
    out = literals.extract("see https://example.com/path?x=1.", span=_SPAN)
    assert values(out, "url") == ["https://example.com/path?x=1"]


def test_url_does_not_double_emit_as_path():
    out = literals.extract("https://api.example.com/v1/items", span=_SPAN)
    assert kinds(out) == ["url"]


def test_path_extraction():
    out = literals.extract("read /etc/hosts and ~/.ssh/config and ./scripts/deploy", span=_SPAN)
    paths = values(out, "path")
    assert "/etc/hosts" in paths
    assert "~/.ssh/config" in paths


def test_route_extraction():
    out = literals.extract("/users/{id}/posts", span=_SPAN)
    assert "route" in kinds(out)


def test_route_requires_path_segment():
    out = literals.extract("/foo", span=_SPAN)
    assert "route" not in kinds(out)


def test_sql_extraction():
    out = literals.extract("SELECT id, name FROM users WHERE active = 1", span=_SPAN)
    assert "sql" in kinds(out)


def test_env_var_only_with_context():
    out_no_ctx = literals.extract("DATABASE_URL", span=_SPAN, context_before="x = ")
    assert "env_var" not in kinds(out_no_ctx)

    out_with_ctx = literals.extract(
        "DATABASE_URL",
        span=_SPAN,
        context_before="os.getenv(",
    )
    assert "env_var" in kinds(out_with_ctx)
