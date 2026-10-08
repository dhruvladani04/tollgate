import asyncio

import pytest
from mcp import types

from tollgate.proxy.intercept import ToolNameCollision, ToolRouter, UnknownTool


def tool(name: str) -> types.Tool:
    return types.Tool(name=name, input_schema={"type": "object"})


def text_result(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


class FakeUpstream:
    def __init__(self, *tool_names: str) -> None:
        self.tool_names = list(tool_names)
        self.calls: list[types.CallToolRequestParams] = []
        self.listings = 0

    async def list_tools(self) -> list[types.Tool]:
        self.listings += 1
        return [tool(name) for name in self.tool_names]

    async def call_tool(self, params: types.CallToolRequestParams) -> types.CallToolResult:
        self.calls.append(params)
        return text_result(f"{params.name} ran")


def call(name: str, **arguments) -> types.CallToolRequestParams:
    return types.CallToolRequestParams(name=name, arguments=arguments)


def test_tools_from_every_upstream_are_listed_in_upstream_order():
    router = ToolRouter({"files": FakeUpstream("read", "write"), "web": FakeUpstream("fetch")})

    tools = asyncio.run(router.list_tools())

    assert [t.name for t in tools] == ["read", "write", "fetch"]


def test_a_call_reaches_only_the_upstream_that_owns_the_tool():
    files, web = FakeUpstream("read"), FakeUpstream("fetch")
    router = ToolRouter({"files": files, "web": web})
    params = call("fetch", url="https://example.com")

    result = asyncio.run(router.call_tool(params))

    assert result == text_result("fetch ran")
    assert web.calls == [params]
    assert files.calls == []


def test_a_call_before_any_listing_still_finds_its_upstream():
    web = FakeUpstream("fetch")
    router = ToolRouter({"web": web})

    asyncio.run(router.call_tool(call("fetch")))

    assert len(web.calls) == 1


def test_a_tool_added_after_the_last_listing_is_found_by_relisting():
    web = FakeUpstream("fetch")
    router = ToolRouter({"web": web})
    asyncio.run(router.list_tools())
    web.tool_names.append("search")

    asyncio.run(router.call_tool(call("search")))

    assert [params.name for params in web.calls] == ["search"]


def test_an_unknown_tool_is_refused_without_calling_any_upstream():
    web = FakeUpstream("fetch")
    router = ToolRouter({"web": web})

    with pytest.raises(UnknownTool, match="delete_everything"):
        asyncio.run(router.call_tool(call("delete_everything")))

    assert web.calls == []


def test_two_upstreams_with_the_same_tool_name_are_refused():
    router = ToolRouter({"files": FakeUpstream("read"), "notes": FakeUpstream("read")})

    with pytest.raises(ToolNameCollision, match="'read'.*'files'.*'notes'"):
        asyncio.run(router.list_tools())
