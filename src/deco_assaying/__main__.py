"""Entrypoint: `python -m deco_assaying` -> uvicorn."""

import argparse
import logging

import anyio
import uvicorn

from deco_assaying.config import HOST, PORT


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="deco-assaying")
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default="http",
        help="MCP transport to use (default: http)",
    )
    return parser.parse_args()


async def _run_stdio() -> None:
    from mcp.server.stdio import stdio_server

    from deco_assaying.routes import mcp

    async with stdio_server() as (read_stream, write_stream):
        await mcp.run(
            read_stream,
            write_stream,
            mcp.create_initialization_options(),
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _parse_args()
    if args.transport == "stdio":
        anyio.run(_run_stdio)
    else:
        uvicorn.run("deco_assaying.app:app", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
