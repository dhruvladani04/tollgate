"""Upstream MCP servers: how they are configured and how Tollgate connects to them.

The config file uses the same `mcpServers` shape as an MCP client config, so an
existing client config can be handed to Tollgate as its list of upstreams.
"""

import json
import os
import re
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

import httpx2
from mcp import Client, StdioServerParameters, types
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tollgate.decide.jev import ROUTES

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
# The MCP SDK's own defaults for streamable HTTP. The read timeout is long
# because a server may hold a response stream open.
_HTTP_TIMEOUT = httpx2.Timeout(30.0, read=300.0)
# Tollgate's own credentials. A stdio upstream never receives these unless the
# upstreams file passes one on by name.
_WITHHELD = frozenset(route.api_key_env for route in ROUTES.values())


class UpstreamConfigError(Exception):
    """The upstreams file is missing, unreadable or malformed."""


class UpstreamConnectError(Exception):
    """An upstream server could not be started or did not complete its handshake."""


class StdioUpstream(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["stdio"] = "stdio"
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


class HttpUpstream(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["http", "streamable-http"] = "http"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)


UpstreamConfig = StdioUpstream | HttpUpstream


def load_upstreams(path: Path, env: Mapping[str, str] | None = None) -> dict[str, UpstreamConfig]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise UpstreamConfigError(f"cannot read {path}: {error.strerror}") from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise UpstreamConfigError(f"{path} is not valid JSON: {error}") from error
    return parse_upstreams(document, env)


def parse_upstreams(
    document: Any, env: Mapping[str, str] | None = None
) -> dict[str, UpstreamConfig]:
    env = os.environ if env is None else env
    servers = document.get("mcpServers") if isinstance(document, dict) else None
    if not isinstance(servers, dict):
        raise UpstreamConfigError('the file needs a top-level "mcpServers" object')
    if not servers:
        raise UpstreamConfigError("mcpServers must name at least one upstream")
    return {name: _parse_entry(name, raw, env) for name, raw in servers.items()}


def _parse_entry(name: str, raw: Any, env: Mapping[str, str]) -> UpstreamConfig:
    where = f"mcpServers.{name}"
    if not isinstance(raw, dict):
        raise UpstreamConfigError(f"{where}: must be an object")
    if "command" in raw:
        model: type[UpstreamConfig] = StdioUpstream
    elif "url" in raw:
        model = HttpUpstream
    else:
        raise UpstreamConfigError(f"{where}: needs a command (stdio) or a url (http)")
    try:
        return model.model_validate(_expand(raw, env, where))
    except ValidationError as error:
        first = error.errors(include_url=False)[0]
        path = ".".join([where, *map(str, first["loc"])])
        raise UpstreamConfigError(f"{path}: {first['msg']}") from error


def _expand(value: Any, env: Mapping[str, str], where: str) -> Any:
    """Replace `${VAR}` and `${VAR:-default}` in every string inside a JSON value."""

    def lookup(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        raise UpstreamConfigError(f"{where}: environment variable {name} is not set")

    if isinstance(value, str):
        return _VARIABLE.sub(lookup, value)
    if isinstance(value, list):
        return [_expand(item, env, where) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, env, where) for key, item in value.items()}
    return value


def upstream_environment(
    config: StdioUpstream, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The environment a stdio upstream starts with.

    It is Tollgate's own environment, which is what the server would get from an
    MCP client directly, minus Tollgate's API keys, plus what the upstreams file sets.
    """
    environ = os.environ if environ is None else environ
    inherited = {name: value for name, value in environ.items() if name.upper() not in _WITHHELD}
    return inherited | config.env


class Upstream:
    """A live connection to one upstream server."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def list_tools(self) -> list[types.Tool]:
        tools: list[types.Tool] = []
        cursor: str | None = None
        while True:
            page = await self._client.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if cursor is None:
                return tools

    async def call_tool(
        self, params: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        # The session-level call hands back an input-required result instead of
        # answering it, so a server's request for input reaches the real client.
        return await self._client.session.call_tool(
            params.name,
            params.arguments,
            input_responses=params.input_responses,
            request_state=params.request_state,
            allow_input_required=True,
        )


@asynccontextmanager
async def connect_upstreams(
    configs: Mapping[str, UpstreamConfig],
) -> AsyncIterator[dict[str, Upstream]]:
    """Start every upstream and keep the connections open for the life of the block."""
    async with AsyncExitStack() as stack:
        upstreams: dict[str, Upstream] = {}
        for name, config in configs.items():
            try:
                upstreams[name] = await _connect(config, stack)
            except Exception as error:
                reason = "; ".join(
                    str(leaf) or type(leaf).__name__ for leaf in leaf_exceptions(error)
                )
                raise UpstreamConnectError(
                    f"upstream {name!r} failed to start: {reason}"
                ) from error
        yield upstreams


async def _connect(config: UpstreamConfig, stack: AsyncExitStack) -> Upstream:
    match config:
        case StdioUpstream():
            server: Any = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=upstream_environment(config),
                cwd=config.cwd,
            )
        case HttpUpstream():
            http_client = await stack.enter_async_context(
                httpx2.AsyncClient(headers=config.headers, timeout=_HTTP_TIMEOUT)
            )
            server = streamable_http_client(config.url, http_client=http_client)
    client = Client(
        server,
        # No response cache: every listing must show what the upstream offers now.
        cache=None,
        client_info=types.Implementation(name="tollgate", version=version("tollgate")),
    )
    return Upstream(await stack.enter_async_context(client))


def leaf_exceptions(error: BaseException) -> Iterator[BaseException]:
    """Yield the exceptions inside an exception group, or the exception itself."""
    if isinstance(error, BaseExceptionGroup):
        for inner in error.exceptions:
            yield from leaf_exceptions(inner)
    else:
        yield error
