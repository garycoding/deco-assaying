"""FastAPI app construction: middleware, MCP transport mount, route wiring.

Logging is configured in `__main__.py` (the entry point); importing this
module does not touch the root logger.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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
# Streamable HTTP MCP transport at /sse, mounted as a raw ASGI3 endpoint so
# Starlette doesn't wrap it in request_response (which would break SSE
# streaming semantics). Same approach as bronze-scribing's server.
app.router.routes.append(Route("/sse", endpoint=mcp_asgi, methods=["GET", "POST", "DELETE"]))
app.include_router(router)
