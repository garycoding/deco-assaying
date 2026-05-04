"""Shared test fixtures.

The MCP `/sse` transport relies on a `StreamableHTTPSessionManager`
that hard-errors on `run()` being called twice. Each `with
TestClient(app) as c` block runs the FastAPI lifespan, which calls
`session_manager.run()`. So we can't have two MCP-using test modules
each owning their own `with`-scoped client — the second module's
lifespan startup blows up.

The fix: a single session-scoped TestClient lives here, every test
module that touches `/sse` reuses it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from deco_assaying.app import app


@pytest.fixture(scope="session")
def mcp_client() -> TestClient:
    """One TestClient for the whole test session — drives the MCP `/sse`
    surface, lifespan started exactly once."""
    with TestClient(app) as c:
        yield c
