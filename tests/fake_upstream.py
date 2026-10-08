"""A small MCP server that stands in for an upstream in proxy tests.

Run as `python fake_upstream.py FLAVOR [PORT]`. With a port it serves streamable
HTTP on 127.0.0.1; without one it serves stdio.
"""

import os
import sys
from typing import Any

from mcp import MCPError, types
from mcp.server import Server, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

REFUSAL = MCPError(code=-32050, message="refused by the upstream", data={"why": "test"})


def build(flavor: str) -> MCPServer | Server[Any]:
    if flavor == "text":
        return _text()
    if flavor == "math":
        return _math()
    if flavor == "strict":
        return _strict()
    if flavor == "env":
        return _env()
    raise ValueError(f"unknown flavor {flavor!r}")


def _text() -> MCPServer:
    server = MCPServer("fake-text")

    @server.tool()
    def echo(text: str) -> str:
        """Return the text unchanged."""
        return text

    @server.tool()
    def fail(reason: str) -> str:
        """Always fail with the given reason."""
        raise ToolError(reason)

    return server


def _math() -> MCPServer:
    server = MCPServer("fake-math")

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    return server


def _env() -> MCPServer:
    server = MCPServer("fake-env")

    @server.tool()
    def lookup(name: str) -> str:
        """Return the value of an environment variable, or an empty string."""
        return os.environ.get(name, "")

    return server


def _strict() -> Server[Any]:
    """A low-level server whose one tool answers with a protocol error."""

    async def list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[types.Tool(name="deny", input_schema={"type": "object"})]
        )

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        raise REFUSAL

    return Server("fake-strict", on_list_tools=list_tools, on_call_tool=call_tool)


if __name__ == "__main__":
    upstream = build(sys.argv[1])
    assert isinstance(upstream, MCPServer)
    if len(sys.argv) > 2:
        upstream.run("streamable-http", host="127.0.0.1", port=int(sys.argv[2]))
    else:
        upstream.run("stdio")
