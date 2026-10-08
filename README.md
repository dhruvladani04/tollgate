# Tollgate

Tollgate is an MCP proxy that checks every tool call an agent makes, and every tool result
coming back, with [Jev](https://docs.typesafe.ai/) before letting it through. It sits
between an MCP client (Claude Code, Cursor, a LangGraph agent) and the upstream MCP servers.
Each call ends one of three ways: allow, ask a human, or block.

**Status: early build.** Tollgate currently forwards every tool call unchanged; it does not
gate anything yet. The decision layer, the policy engine and the evaluation are still to
come; see the milestones in [docs/spec.md](docs/spec.md).

Tollgate is not a security guarantee. Jev's vendor says adversarial content can move its
answers, so Tollgate is one layer of defense, not the only one.

## Set up

Tollgate needs [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 for you.

```text
uv sync
uv run pytest
```

The tests use no network and need no API key. A few of them start real server processes, so
the run takes about 20 seconds.

## Run the proxy

Tollgate reads its upstream servers from a JSON file with the same `mcpServers` shape that
MCP clients use, so an existing client config works as it is.
[examples/upstreams.json](examples/upstreams.json) names two upstreams:

- the reference filesystem server, limited to the `examples/sandbox` folder (needs Node.js)
- the reference fetch server (started with `uvx`, which comes with uv)

To use Tollgate from Claude Code, start Claude Code in the repo root with the example
client config:

```text
claude --mcp-config examples/claude_code.mcp.json
```

`/mcp` should then show `tollgate` with 15 tools: 14 from the filesystem server and `fetch`.
Starting takes several seconds, because Tollgate starts each upstream before it answers.

### How upstreams are treated

- **Tool names are unchanged.** If two upstreams offer a tool with the same name, Tollgate
  refuses to start and names both.
- **Environment.** A stdio upstream inherits Tollgate's environment, as it would from an MCP
  client, except for Tollgate's own API keys. To set or pass a variable, use `env` in the
  upstreams file; `${NAME}` and `${NAME:-default}` are expanded.
- **Transports.** Upstreams can be stdio commands or streamable HTTP URLs. Tollgate itself
  serves stdio.
- **Tools only.** Resources, prompts and upstream notifications are not forwarded yet.

### Known issue with the fetch server on Windows

On Windows with Node.js installed, the reference fetch server can hang while it converts a
page to Markdown. This happens without Tollgate too. Pass `"raw": true` to the `fetch` tool
to skip the conversion.

## Make one live Jev call

The smoke test sends one request that asks a Noul, a Score and a Choice question, then
prints the typed answers. It needs an API key, through either route:

| Route | Set these variables |
| --- | --- |
| TypeSafe directly (the default) | `TYPESAFE_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` and `TOLLGATE_JEV_ROUTE=openrouter` |

Put them in a file named `.env` in the repo root, one `NAME=value` per line. Git ignores
that file. Then run:

```text
uv run --env-file .env python scripts/smoke_jev.py
```

Both routes pin Jev to version 1.13.0, so that thresholds tuned later stay valid.
