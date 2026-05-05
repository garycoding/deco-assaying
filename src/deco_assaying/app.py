"""FastAPI app construction: middleware, MCP transport mount, route wiring.

Logging is configured in `__main__.py` (the entry point); importing this
module does not touch the root logger.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.routing import Route

from deco_assaying.config import VERSION
from deco_assaying.routes import (
    lifespan,
    mcp_asgi,
    router,
)

app = FastAPI(
    title="deco-assaying",
    version=VERSION,
    description=(
        "Tree-sitter-based source code analysis MCP server. The /admin/* "
        "endpoints expose read-only ops information; job control is on "
        "the MCP /sse surface."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Gzip JSON-heavy responses when the client sent Accept-Encoding:
# gzip. The threshold is the size at which gzip's framing overhead
# (~20 bytes) is comfortably beaten by compression savings — 256
# bytes is conservative; nginx defaults to 20. Big wins on the
# download API (rollups, per-file analyses); incidental wins on
# MCP /sse. Tiny health/admin responses stay uncompressed.
app.add_middleware(GZipMiddleware, minimum_size=256)
# Streamable HTTP MCP transport at /sse, mounted as a raw ASGI3 endpoint so
# Starlette doesn't wrap it in request_response (which would break SSE
# streaming semantics). Same approach as bronze-scribing's server.
app.router.routes.append(Route("/sse", endpoint=mcp_asgi, methods=["GET", "POST", "DELETE"]))
app.include_router(router)
