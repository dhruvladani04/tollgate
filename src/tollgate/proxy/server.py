"""Tollgate's server face: the MCP server a client connects to."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Mapping
from contextlib import AsyncExitStack
from importlib.metadata import version
from pathlib import Path
from typing import Any

from mcp import MCPError, types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from typesafe_sdk import TypeSafeError

from tollgate.audit.store import AuditStore
from tollgate.decide.cache import CachedDecisionModel, DecisionCache
from tollgate.decide.jev import (
    JevConfigError,
    JevDecisionModel,
    JevRoute,
    make_client,
    read_api_key,
    resolve_route,
)
from tollgate.proxy.intercept import (
    DecisionObserver,
    RoutingError,
    ToolNameCollision,
    ToolRouter,
    ToolSource,
)
from tollgate.proxy.upstreams import (
    UpstreamConfig,
    UpstreamConfigError,
    UpstreamConnectError,
    connect_upstreams,
    leaf_exceptions,
    load_upstreams,
)

logger = logging.getLogger("tollgate")

AUDIT_FILE = "audit.sqlite"
CACHE_FILE = "jev-cache.jsonl"


def build_server(source: ToolSource) -> Server[Any]:
    async def list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        try:
            return types.ListToolsResult(tools=await source.list_tools())
        except RoutingError as error:
            raise MCPError(error.code, str(error)) from error

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        try:
            return await source.call_tool(params)
        except RoutingError as error:
            raise MCPError(error.code, str(error)) from error

    return Server(
        "tollgate",
        version=version("tollgate"),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve(
    configs: Mapping[str, UpstreamConfig], route: JevRoute | None, state_dir: Path
) -> None:
    """Connect to every upstream, then serve one client over stdin and stdout.

    With a Jev route, every call is put to Jev and recorded before it is
    forwarded. Without one, calls are forwarded and nothing is recorded.
    """
    async with AsyncExitStack() as stack:
        observed_with = None
        if route is not None:
            # Opened before the upstreams, so that a bad key is reported plainly
            # and no upstream is started for nothing.
            client = await stack.enter_async_context(make_client(route))
            audit = stack.enter_context(AuditStore(state_dir / AUDIT_FILE))
            model = CachedDecisionModel(
                JevDecisionModel(client, route.model), DecisionCache(state_dir / CACHE_FILE)
            )
            observed_with = (model, audit)

        upstreams = await stack.enter_async_context(connect_upstreams(configs))
        router = ToolRouter(upstreams)
        tools = await router.list_tools()
        logger.info(
            "forwarding %d tools from %d upstreams (%s)",
            len(tools),
            len(upstreams),
            ", ".join(upstreams),
        )

        source: ToolSource = router
        if observed_with is None:
            logger.info("passthrough mode: no decisions are made and nothing is recorded")
        else:
            source = DecisionObserver(router, *observed_with)
            logger.info(
                "observe mode: asking %s over the %s route, recording to %s, blocking nothing",
                route.model,
                route.name,
                state_dir / AUDIT_FILE,
            )

        server = build_server(source)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tollgate",
        description="MCP proxy that puts tool calls to a decision model before forwarding them.",
    )
    parser.add_argument(
        "--upstreams",
        type=Path,
        required=True,
        metavar="FILE",
        help="JSON file whose mcpServers map names the upstream servers",
    )
    parser.add_argument(
        "--mode",
        choices=("enforce", "observe", "passthrough"),
        default="enforce",
        help=(
            "enforce: gate calls (not built yet). observe: record a decision for every "
            "call but block nothing. passthrough: forward only. Default: %(default)s"
        ),
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".tollgate"),
        metavar="DIR",
        help="where the audit log and the decision cache are kept (default: %(default)s)",
    )
    args = parser.parse_args()

    # stdout carries the MCP protocol, so every log line goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(name)s: %(message)s")
    logger.setLevel(logging.INFO)

    if args.mode == "enforce":
        sys.exit(
            "tollgate: enforce mode is not built yet; run with --mode observe or --mode passthrough"
        )
    try:
        configs = load_upstreams(args.upstreams)
        route = resolve_route() if args.mode == "observe" else None
        if route is not None:
            read_api_key(route)
        asyncio.run(serve(configs, route, args.state_dir))
    except (UpstreamConfigError, JevConfigError) as error:
        sys.exit(f"tollgate: {error}")
    except KeyboardInterrupt:
        pass
    except Exception as error:
        # Errors raised while upstream connections are open arrive inside exception groups.
        for leaf in leaf_exceptions(error):
            if isinstance(leaf, UpstreamConnectError | ToolNameCollision | TypeSafeError):
                sys.exit(f"tollgate: {leaf}")
        raise


if __name__ == "__main__":
    main()
