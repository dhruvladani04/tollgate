# Tollgate

Tollgate is an MCP proxy that checks every tool call an agent makes, and every tool result
coming back, with [Jev](https://docs.typesafe.ai/) before letting it through. It sits
between an MCP client (Claude Code, Cursor, a LangGraph agent) and the upstream MCP servers.
Each call ends one of three ways: allow, ask a human, or block.

**Status: early build.** Only the project scaffold and a Jev smoke test exist so far. The
proxy, the policy engine and the evaluation are still to come; see the milestones in
[docs/spec.md](docs/spec.md).

Tollgate is not a security guarantee. Jev's vendor says adversarial content can move its
answers, so Tollgate is one layer of defense, not the only one.

## Set up

Tollgate needs [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 for you.

```text
uv sync
uv run pytest
```

The tests use no network and need no API key.

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
