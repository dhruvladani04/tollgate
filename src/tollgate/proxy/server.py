"""Tollgate's server face: the MCP server a client connects to."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path
from typing import Any

from mcp import MCPError, types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server

from tollgate.proxy.intercept import RoutingError, ToolNameCollision, ToolRouter
from tollgate.proxy.upstreams import (
    UpstreamConfig,
    UpstreamConfigError,
    UpstreamConnectError,
    connect_upstreams,
    leaf_exceptions,
    load_upstreams,
)

logger = logging.getLogger("tollgate")


def build_server(router: ToolRouter) -> Server[Any]:
    async def list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        try:
            return types.ListToolsResult(tools=await router.list_tools())
        except RoutingError as error:
            raise MCPError(error.code, str(error)) from error

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        try:
            return await router.call_tool(params)
        except RoutingError as error:
            raise MCPError(error.code, str(error)) from error

    return Server(
        "tollgate",
        version=version("tollgate"),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve(configs: Mapping[str, UpstreamConfig]) -> None:
    """Connect to every upstream, then serve one client over stdin and stdout."""
    async with connect_upstreams(configs) as upstreams:
        router = ToolRouter(upstreams)
        tools = await router.list_tools()
        logger.info(
            "forwarding %d tools from %d upstreams (%s)",
            len(tools),
            len(upstreams),
            ", ".join(upstreams),
        )
        server = build_server(router)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tollgate",
        description="MCP proxy that forwards tool calls to upstream MCP servers.",
    )
    parser.add_argument(
        "--upstreams",
        type=Path,
        required=True,
        metavar="FILE",
        help="JSON file whose mcpServers map names the upstream servers",
    )
    args = parser.parse_args()

    # stdout carries the MCP protocol, so every log line goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(name)s: %(message)s")
    logger.setLevel(logging.INFO)

    try:
        asyncio.run(serve(load_upstreams(args.upstreams)))
    except UpstreamConfigError as error:
        sys.exit(f"tollgate: {error}")
    except KeyboardInterrupt:
        pass
    except Exception as error:
        # Errors raised while upstream connections are open arrive inside exception groups.
        for leaf in leaf_exceptions(error):
            if isinstance(leaf, UpstreamConnectError | ToolNameCollision):
                sys.exit(f"tollgate: {leaf}")
        raise


if __name__ == "__main__":
    main()
