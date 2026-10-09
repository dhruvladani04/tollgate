# Tollgate

Tollgate is an MCP proxy that checks every tool call an agent makes, and every tool result
coming back, with [Jev](https://docs.typesafe.ai/) before letting it through. It sits
between an MCP client (Claude Code, Cursor, a LangGraph agent) and the upstream MCP servers.
Each call ends one of three ways: allow, ask a human, or block.

**Status: early build.** Tollgate can observe: it asks Jev about every tool call and
records the answer, but it does not block anything yet. The policy engine that acts on the
answers and the evaluation are still to come; see the milestones in
[docs/spec.md](docs/spec.md).

Tollgate is not a security guarantee. Jev's vendor says adversarial content can move its
answers, so Tollgate is one layer of defense, not the only one.

## Set up

Tollgate needs [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 for you.

```text
uv sync
uv run pytest
```

The tests use no network and need no API key. A few of them start real server processes, so
the run takes about 45 seconds.

## Give Tollgate a Jev key

Observing needs an API key for Jev, through either route:

| Route | Set these variables |
| --- | --- |
| TypeSafe directly (the default) | `TYPESAFE_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` and `TOLLGATE_JEV_ROUTE=openrouter` |

Put them in a file named `.env` in the repo root, one `NAME=value` per line. Git ignores
that file. Both routes pin Jev to version 1.13.0, so that thresholds tuned later stay valid.

To check the key, send one request that asks a Noul, a Score and a Choice question:

```text
uv run --env-file .env python scripts/smoke_jev.py
```

## Run the proxy

Tollgate reads its upstream servers from a JSON file with the same `mcpServers` shape that
MCP clients use, so an existing client config works as it is.
[examples/upstreams.json](examples/upstreams.json) names two upstreams:

- the reference filesystem server, limited to the `examples/sandbox` folder (needs Node.js)
- the reference fetch server (started with `uvx`, which comes with uv)

`--mode` chooses what Tollgate does with each call:

| Mode | What happens |
| --- | --- |
| `observe` | Jev is asked about the call, the answer is recorded, and the call is forwarded whatever the answer. |
| `passthrough` | The call is forwarded. Nothing is asked or recorded. Needs no key. |
| `enforce` | The default. Not built yet, so Tollgate exits with a message. |

To use Tollgate from Claude Code in observe mode, start Claude Code in the repo root with
the example client config, which loads `.env`:

```text
claude --mcp-config examples/claude_code.mcp.json
```

`/mcp` should then show `tollgate` with 15 tools: 14 from the filesystem server and `fetch`.
Starting takes several seconds, because Tollgate starts each upstream before it answers.

## See what was recorded

Every call in observe mode writes one row to `.tollgate/audit.sqlite`: the tool and its
arguments, Jev's probability for each question, the model version that answered, the time
the decision took and the tokens it used. To print the latest rows:

```text
uv run python -m tollgate.audit
```

To see the whole loop without an MCP client of your own, run the demo. It sends five calls
through Tollgate and prints the audit log:

```text
uv run --env-file .env python scripts/observe_demo.py
```

Identical requests are answered from a cache in `.tollgate/jev-cache.jsonl`, so a repeated
call costs nothing and takes no time.

## Tell Tollgate what the user asked for

MCP sends a server the tool name and the arguments, and nothing about the conversation.
Two of Tollgate's four questions need the user's request: whether the call serves it, and
whether the call sends data somewhere the request never named. A client that knows the
request passes it in the `_meta` of each tool call:

| `_meta` key | Value |
| --- | --- |
| `io.github.dhruvladani04.tollgate/user_request` | The user's request, as a string |
| `io.github.dhruvladani04.tollgate/recent_context` | Recent turns, as a list of strings; the last two are used |

Without them, Tollgate asks only how risky the call is and whether it destroys data. Claude
Code does not send them, so calls from Claude Code get those two answers only.

## How upstreams are treated

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
