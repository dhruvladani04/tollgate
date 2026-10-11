"""Tollgate's server face: the MCP server a client connects to."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from mcp import MCPError, types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from typesafe_sdk import RetryPolicy, TypeSafeError

from tollgate.approve.queue import APPROVALS_FILE, ApprovalQueue
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
from tollgate.decide.llm import Judge, LlmConfigError, LlmDecisionModel
from tollgate.policy.schema import Policy, PolicyError, load_policy
from tollgate.prompts import PROMPTS_FILE, PromptLog
from tollgate.proxy.intercept import (
    ClientChannel,
    Gate,
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


@dataclass(frozen=True)
class Gating:
    """What the gate needs beyond the upstreams. Absent in passthrough mode."""

    route: JevRoute
    policy: Policy
    enforce: bool
    started: datetime
    """When this process started. Prompts recorded before then belong to an earlier session."""


def client_channel(ctx: ServerRequestContext[Any]) -> ClientChannel:
    """Work out how this client's user can be asked about a held call, if at all."""
    capabilities = ctx.session.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    # An empty declaration means form mode, the only mode that existed at first.
    if elicitation is None or (elicitation.form is None and elicitation.url is not None):
        return ClientChannel()
    if ctx.session.can_send_request:

        async def elicit(message: str, schema: dict[str, Any]) -> types.ElicitResult:
            return await ctx.session.elicit_form(message, schema, related_request_id=ctx.request_id)

        return ClientChannel(elicit=elicit)
    return ClientChannel(round_trip=ctx.protocol_version in MODERN_PROTOCOL_VERSIONS)


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
            return await source.call_tool(params, client_channel(ctx))
        except RoutingError as error:
            raise MCPError(error.code, str(error)) from error

    return Server(
        "tollgate",
        version=version("tollgate"),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def second_model_for(policy: Policy) -> Any:
    """The second model the policy names, for calls the first leaves uncertain."""
    settings = policy.escalation
    budget = settings.timeout_ms / 1000
    judge = Judge(settings.model, settings.thinking, settings.corrective_retries, settings.provider)
    return LlmDecisionModel.for_judge(
        judge, retry=RetryPolicy(max_retries=policy.decision.max_retries, timeout=budget)
    )


async def serve(
    configs: Mapping[str, UpstreamConfig], gating: Gating | None, state_dir: Path
) -> None:
    """Connect to every upstream, then serve one client over stdin and stdout.

    With `gating`, every call is judged and recorded, and in enforce mode acted
    on. Without it, calls are forwarded and nothing is recorded.
    """
    async with AsyncExitStack() as stack:
        gate_parts = None
        if gating is not None:
            # Opened before the upstreams, so that a bad key is reported plainly
            # and no upstream is started for nothing.
            budget = gating.policy.decision.timeout_ms / 1000
            client = await stack.enter_async_context(
                make_client(
                    gating.route,
                    timeout=budget,
                    retry=RetryPolicy(
                        max_retries=gating.policy.decision.max_retries, timeout=budget
                    ),
                )
            )
            audit = stack.enter_context(AuditStore(state_dir / AUDIT_FILE))
            approvals = (
                stack.enter_context(ApprovalQueue(state_dir / APPROVALS_FILE))
                if gating.enforce
                else None
            )
            cache = DecisionCache(state_dir / CACHE_FILE)
            model = CachedDecisionModel(JevDecisionModel(client, gating.route.model), cache)
            second_model = None
            if gating.policy.escalation.enabled:
                judge = second_model_for(gating.policy)
                stack.push_async_callback(judge.aclose)
                # Its answers share the cache; the key names the model, so they never mix.
                second_model = CachedDecisionModel(judge, cache)
            gate_parts = (model, audit, gating.policy, approvals)

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
        if gating is None or gate_parts is None:
            logger.info("passthrough mode: no decisions are made and nothing is recorded")
        else:
            prompts = PromptLog(state_dir / PROMPTS_FILE)
            source = Gate(
                router,
                *gate_parts,
                enforce=gating.enforce,
                ambient_context=lambda: prompts.context(gating.started),
                second_model=second_model,
            )
            if second_model is not None:
                logger.info(
                    "calls the first model is unsure about go to %s before a person",
                    second_model.model,
                )
            logger.info(
                "%s mode: asking %s over the %s route, recording to %s. %s",
                "enforce" if gating.enforce else "observe",
                gating.route.model,
                gating.route.name,
                state_dir / AUDIT_FILE,
                "Held calls wait for the client's user or for `tollgate-approve`."
                if gating.enforce
                else "Nothing is blocked.",
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
            "enforce: allow, hold or block each call. observe: record what enforce would "
            "do but block nothing. passthrough: forward only. Default: %(default)s"
        ),
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("policy.yaml"),
        metavar="FILE",
        help="policy file for enforce and observe modes (default: %(default)s)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".tollgate"),
        metavar="DIR",
        help="where the audit log, held calls and decision cache are kept (default: %(default)s)",
    )
    args = parser.parse_args()
    started = datetime.now(UTC)

    # stdout carries the MCP protocol, so every log line goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(name)s: %(message)s")
    logger.setLevel(logging.INFO)

    try:
        configs = load_upstreams(args.upstreams)
        gating = None
        if args.mode != "passthrough":
            gating = Gating(
                route=resolve_route(),
                policy=load_policy(args.policy),
                enforce=args.mode == "enforce",
                started=started,
            )
            read_api_key(gating.route)
        asyncio.run(serve(configs, gating, args.state_dir))
    except (UpstreamConfigError, JevConfigError, LlmConfigError, PolicyError) as error:
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
