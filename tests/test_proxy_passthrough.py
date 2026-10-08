"""A client must see the same tools and results through Tollgate as it sees directly."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import fake_upstream
import pytest
from mcp import Client, MCPError, types

from tollgate.proxy.intercept import ToolRouter
from tollgate.proxy.server import build_server
from tollgate.proxy.upstreams import Upstream


@asynccontextmanager
async def proxy_over(*flavors: str) -> AsyncIterator[Client]:
    """A client connected to Tollgate, which is connected to one fake upstream per flavor."""
    async with AsyncExitStack() as stack:
        upstreams = {}
        for flavor in flavors:
            upstream = Client(fake_upstream.build(flavor), cache=None)
            upstreams[flavor] = Upstream(await stack.enter_async_context(upstream))
        server = build_server(ToolRouter(upstreams))
        yield await stack.enter_async_context(Client(server, cache=None))


async def direct_tools(flavor: str) -> list[types.Tool]:
    async with Client(fake_upstream.build(flavor), cache=None) as client:
        return (await client.list_tools()).tools


async def direct_call(flavor: str, name: str, arguments: dict) -> types.CallToolResult:
    async with Client(fake_upstream.build(flavor), cache=None) as client:
        return await client.call_tool(name, arguments)


def test_listed_tools_equal_the_upstreams_own_listings():
    async def scenario():
        async with proxy_over("text", "math") as client:
            proxied = (await client.list_tools()).tools
        return proxied, await direct_tools("text") + await direct_tools("math")

    proxied, direct = asyncio.run(scenario())

    assert [tool.name for tool in proxied] == ["echo", "fail", "add"]
    assert proxied == direct


@pytest.mark.parametrize(
    ("flavor", "name", "arguments"),
    [
        ("text", "echo", {"text": "héllo → world"}),
        ("math", "add", {"a": 2, "b": 3}),
        ("text", "fail", {"reason": "disk full"}),
        ("math", "add", {"a": "two", "b": 3}),
    ],
    ids=["text-result", "structured-result", "tool-error", "invalid-arguments"],
)
def test_a_call_returns_exactly_what_the_upstream_returns(flavor, name, arguments):
    async def scenario():
        async with proxy_over("text", "math") as client:
            proxied = await client.call_tool(name, arguments)
        return proxied, await direct_call(flavor, name, arguments)

    proxied, direct = asyncio.run(scenario())

    assert proxied == direct


def test_the_fixture_results_are_the_ones_the_cases_claim():
    """Guards the parametrized test above against comparing two identical failures."""

    async def scenario():
        async with proxy_over("text", "math") as client:
            return (
                await client.call_tool("echo", {"text": "hi"}),
                await client.call_tool("add", {"a": 2, "b": 3}),
                await client.call_tool("fail", {"reason": "disk full"}),
            )

    echoed, added, failed = asyncio.run(scenario())

    assert echoed.content == [types.TextContent(type="text", text="hi")]
    assert not echoed.is_error
    assert added.structured_content == {"result": 5}
    assert failed.is_error
    assert "disk full" in failed.content[0].text


def test_an_upstream_protocol_error_keeps_its_code_message_and_data():
    async def scenario():
        async with proxy_over("strict") as client:
            with pytest.raises(MCPError) as caught:
                await client.call_tool("deny", {})
            return caught.value

    error = asyncio.run(scenario())

    assert error.error == fake_upstream.REFUSAL.error


def test_an_unknown_tool_is_an_invalid_params_error():
    async def scenario():
        async with proxy_over("text") as client:
            with pytest.raises(MCPError) as caught:
                await client.call_tool("delete_everything", {})
            return caught.value

    error = asyncio.run(scenario())

    assert error.code == types.INVALID_PARAMS
    assert error.message == "Unknown tool: delete_everything"
