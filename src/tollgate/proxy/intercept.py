"""The one path every tool listing and tool call takes on its way to an upstream."""

import logging
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from mcp import types

from tollgate.audit.models import AuditRecord
from tollgate.audit.store import AuditStore
from tollgate.decide.base import Decision, DecisionError, DecisionModel
from tollgate.decide.cache import request_key
from tollgate.decide.questions import call_request, context_from_meta

logger = logging.getLogger("tollgate")

CallResult = types.CallToolResult | types.InputRequiredResult


class RoutingError(Exception):
    """A tool could not be routed. `code` is the JSON-RPC error code the client gets."""

    code = types.INTERNAL_ERROR


class UnknownTool(RoutingError):
    code = types.INVALID_PARAMS


class ToolNameCollision(RoutingError):
    pass


class ToolSource(Protocol):
    async def list_tools(self) -> list[types.Tool]: ...

    async def call_tool(self, params: types.CallToolRequestParams) -> CallResult: ...


@dataclass(frozen=True)
class RoutedTool:
    upstream: str
    tool: types.Tool


class ToolRouter:
    """Presents the tools of several upstreams as one list, under their own names."""

    def __init__(self, upstreams: Mapping[str, ToolSource]) -> None:
        self._upstreams = upstreams
        self._tools: dict[str, RoutedTool] = {}

    async def list_tools(self) -> list[types.Tool]:
        """Ask every upstream for its tools and remember which upstream owns each name."""
        tools: dict[str, RoutedTool] = {}
        for upstream_name, upstream in self._upstreams.items():
            for tool in await upstream.list_tools():
                if tool.name in tools:
                    raise ToolNameCollision(
                        f"tool {tool.name!r} is offered by both "
                        f"{tools[tool.name].upstream!r} and {upstream_name!r}"
                    )
                tools[tool.name] = RoutedTool(upstream_name, tool)
        self._tools = tools
        return [routed.tool for routed in tools.values()]

    async def find(self, name: str) -> RoutedTool:
        """The tool with this name and its upstream, listing again if the name is new."""
        if name not in self._tools:
            await self.list_tools()
        routed = self._tools.get(name)
        if routed is None:
            raise UnknownTool(f"Unknown tool: {name}")
        return routed

    async def call_tool(self, params: types.CallToolRequestParams) -> CallResult:
        routed = await self.find(params.name)
        return await self._upstreams[routed.upstream].call_tool(params)


class DecisionObserver:
    """Asks the model about every call, records the answer, then forwards the call anyway.

    This is observe-only mode. Nothing here can stop a call or change its result:
    a model failure or an audit failure is logged and the call still goes through.
    """

    mode = "observe"

    def __init__(self, router: ToolRouter, model: DecisionModel, audit: AuditStore) -> None:
        self._router = router
        self._model = model
        self._audit = audit

    async def list_tools(self) -> list[types.Tool]:
        return await self._router.list_tools()

    async def call_tool(self, params: types.CallToolRequestParams) -> CallResult:
        routed = await self._router.find(params.name)
        context = context_from_meta(params.meta)
        request = call_request(routed.tool, params.arguments, context)

        decision: Decision | None = None
        error: str | None = None
        started = time.perf_counter()
        try:
            decision = await self._model.decide(request)
        except DecisionError as failure:
            error = f"{failure.kind}: {failure}"
        except Exception as failure:
            logger.exception("the decision model failed unexpectedly")
            error = f"failed: {failure}"
        latency_ms = (time.perf_counter() - started) * 1000

        record = AuditRecord(
            timestamp=datetime.now(UTC),
            mode=self.mode,
            action="forwarded",
            upstream=routed.upstream,
            tool=params.name,
            arguments=params.arguments,
            user_request=context.user_request,
            request_key=request_key(self._model.backend, self._model.model, request),
            backend=self._model.backend,
            model=decision.model if decision else None,
            cached=decision.cached if decision else False,
            latency_ms=latency_ms,
            input_tokens=decision.input_tokens if decision else None,
            output_tokens=decision.output_tokens if decision else None,
            answers=decision.answers if decision else {},
            error=error,
        )
        try:
            self._audit.write(record)
        except sqlite3.Error:
            logger.exception("could not write the audit record for %s", params.name)
        return await self._router.call_tool(params)
