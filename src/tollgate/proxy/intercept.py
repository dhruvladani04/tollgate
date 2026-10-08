"""The one path every tool listing and tool call takes on its way to an upstream."""

from collections.abc import Mapping
from typing import Protocol

from mcp import types


class RoutingError(Exception):
    """A tool could not be routed. `code` is the JSON-RPC error code the client gets."""

    code = types.INTERNAL_ERROR


class UnknownTool(RoutingError):
    code = types.INVALID_PARAMS


class ToolNameCollision(RoutingError):
    pass


class ToolSource(Protocol):
    async def list_tools(self) -> list[types.Tool]: ...

    async def call_tool(
        self, params: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult: ...


class ToolRouter:
    """Presents the tools of several upstreams as one list, under their own names."""

    def __init__(self, upstreams: Mapping[str, ToolSource]) -> None:
        self._upstreams = upstreams
        self._owners: dict[str, str] = {}

    async def list_tools(self) -> list[types.Tool]:
        """Ask every upstream for its tools and remember which upstream owns each name."""
        owners: dict[str, str] = {}
        tools: list[types.Tool] = []
        for upstream_name, upstream in self._upstreams.items():
            for tool in await upstream.list_tools():
                if tool.name in owners:
                    raise ToolNameCollision(
                        f"tool {tool.name!r} is offered by both "
                        f"{owners[tool.name]!r} and {upstream_name!r}"
                    )
                owners[tool.name] = upstream_name
                tools.append(tool)
        self._owners = owners
        return tools

    async def call_tool(
        self, params: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        if params.name not in self._owners:
            await self.list_tools()
        owner = self._owners.get(params.name)
        if owner is None:
            raise UnknownTool(f"Unknown tool: {params.name}")
        return await self._upstreams[owner].call_tool(params)
