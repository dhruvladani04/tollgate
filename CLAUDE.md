# Tollgate

An MCP proxy that checks every tool call, and every tool result, with TypeSafe's Jev model
before letting it through. Each call ends one of three ways: allow, ask a human, or block.

The full spec is `docs/spec.md`. Read it completely before changing the design.

## How to work

- Build one milestone per session (M0 to M6 in the spec). At the end of a milestone, stop,
  show how to verify its "Done when" condition, and wait for go-ahead.
- Use the TypeSafe skill and the live docs (index: https://docs.typesafe.ai/llms.txt) for
  anything about the Jev API. Do not guess request shapes. The installed SDK under
  `.venv/Lib/site-packages/typesafe_sdk` settles any disagreement.
- Python 3.12 with uv. Ask before adding a dependency the spec does not list.
- Write tests first for the policy engine and the pre-filter; they are pure functions.

## Rules from the spec

- **Pinned model.** Jev is pinned to version 1.13.0. Never use `jev-latest` or `jev-preview`
  in code or tests; `tests/test_jev_config.py` fails if either appears. The pinned ID for
  each route lives only in `src/tollgate/decide/jev.py`.
- **Code decides what code can decide.** Allowlists, path checks, secret regexes, size
  limits, arithmetic and date comparison never go to the model.
- **One judgment per Jev question.** Combine answers in the policy engine, not in prompts.
- **Small state.** Send Jev only `user_request`, `proposed_call` and trimmed `recent_context`.
- **Fail closed.** A Jev timeout, or a 429 after retries, means Ask, never Allow.
- **No magic numbers.** Every threshold lives in the policy YAML.
- **Keys stay out of the repo.** Code reads keys from the environment only. Local keys
  live in the gitignored `.env`, loaded with `uv run --env-file .env`. Do not read that
  file, and never put a key in a tracked file or a log.
- **Cache by exact request** so tests and evals do not re-spend.

## Jev access

`TOLLGATE_JEV_ROUTE` selects the route; the default is `typesafe`.

| Route | Key variable | Pinned model ID | Base URL |
| --- | --- | --- | --- |
| `typesafe` | `TYPESAFE_API_KEY` | `jev-1.13.0` | `https://api.typesafe.ai` |
| `openrouter` | `OPENROUTER_API_KEY` | `typesafe/jev-1.13` | `https://openrouter.ai/api` |

Both routes speak the same System One API through `typesafe-sdk`. TypeSafe's own console
is early access; OpenRouter needs only an OpenRouter key.

## Commands

```text
uv sync                               install dependencies
uv run pytest                         run the tests (no network, no key needed)
uv run ruff check .                   lint
uv run ruff format .                  format
uv run --env-file .env python scripts/smoke_jev.py    one live Jev call
uv run --env-file .env python scripts/observe_demo.py         M2 demo: calls plus audit log
uv run --env-file .env python scripts/measure_jev_latency.py  p50 and p95 of Jev from here
uv run python -m tollgate.audit                       print the latest audit records
uv run --env-file .env tollgate --upstreams examples/upstreams.json --mode observe
```

`.env` sets `TOLLGATE_JEV_ROUTE` and the key for that route. A few tests start real
server processes, so the full run takes about 45 seconds.

## The proxy

- **The `mcp` SDK is version 2.** Its API differs from version 1: the low-level `Server`
  takes `on_list_tools` and `on_call_tool` handlers, `mcp.Client` replaces hand-built
  sessions, and `FastMCP` is now `MCPServer`. Read the installed source under
  `.venv/Lib/site-packages/mcp` instead of relying on memory.
- **One path for every call.** `ToolRouter` in `proxy/intercept.py` is the only route from
  the client to an upstream. Gating belongs there.
- **Tool names are unchanged.** If two upstreams offer the same name, Tollgate refuses to
  start rather than guess.
- **Upstream environment.** A stdio upstream inherits Tollgate's environment minus
  Tollgate's own API keys (`_WITHHELD` in `proxy/upstreams.py`, built from the Jev routes).
  When a milestone adds another key, such as the LLM judge's, add its variable there.
- **Errors cross client blocks in groups.** An exception raised inside `async with
  Client(...)` reaches the outside wrapped in an `ExceptionGroup`. Catch it inside the
  block, or unwrap it with `leaf_exceptions`.
- **stdout is the protocol.** Log to stderr only.
- **Only tools are forwarded so far.** Resources, prompts and upstream notifications are not.
- **Modes.** `--mode passthrough` forwards only. `--mode observe` asks Jev about every
  call, writes an audit row, and forwards the call whatever the answer. `--mode enforce`
  is the default and exits with a message until M3 builds it.

## The decision layer

- **One interface.** `DecisionModel` in `decide/base.py` takes a `DecisionRequest`, which
  is a System One request: a `state` plus named questions as plain wire-shape dicts. Any
  backend that answers that shape can stand behind the gate, which is how the LLM judge
  arrives in M5.
- **Questions.** `decide/questions.py` holds the spec's wording. Jev judges each Score
  level on its own, without seeing the other levels, so each level describes a whole
  situation. Read the Jev docs (Score, Noul, jaggedness) before rewording anything.
- **The user's request is not in MCP.** A tool call carries a tool name and arguments,
  nothing about the conversation. A client that knows the user's request sends it in the
  call's `_meta` (keys in `decide/questions.py`). Without it, only `risk` and
  `destructive` are asked, because `on_task` and `exfiltration` need `user_request`.
  Claude Code sends no such context.
- **Arguments go to Jev whole.** Size limits belong to the M3 policy and pre-filter.
- **Cache.** `decide/cache.py` keys on the exact request and stores JSON lines, so an
  evaluation can commit its cache. Jev's answers to the same request vary by a few
  hundredths between calls; a cached answer does not.
- **Audit.** `audit/store.py` writes one SQLite row per call. It uses SQLite's default
  journal mode. With write-ahead logging, opening the file while Tollgate was shutting
  down failed once on Windows with a disk I/O error; the cause was not confirmed, so
  test on Windows before turning it back on.
- **Observe mode blocks nothing.** A model failure or an audit failure is recorded or
  logged, and the call is still forwarded.

## Layout

```text
docs/spec.md          the build plan
docs/decisions/       short ADRs
src/tollgate/
  proxy/              MCP server face and upstream sessions (M1)
  decide/             DecisionModel protocol, questions, cache, Jev backend (M2); LLM (M5)
  policy/             YAML schema, pre-filter, band engine (M3)
  approve/            approval queue and CLI (M3)
  audit/              SQLite audit log (M2)
  dashboard/          FastAPI dashboard (M6)
evals/                golden data, runner, tuning, report (M4)
scripts/              developer scripts, such as the Jev smoke test
tests/
examples/             upstreams file, Claude Code config, sandbox folder (M1); demos (M6)
```

## Decided with the user

- **Claude Code hook, after M3 (decided 9 Oct 2026).** Once M3's core is done, add a
  `UserPromptSubmit` hook that hands each prompt to Tollgate as `user_request`. It is
  its own small step, not part of M3. Limits to handle when building it: the latest
  prompt is often not the task (send the last two prompts as context), prompts can only
  be matched to a session by project folder, and a stale prompt needs a freshness rule.
- **M3 must work without a user request.** Generic clients never send one, so the policy
  needs defined behaviour when `on_task` and `exfiltration` were not asked.

Proposed by Claude and not yet confirmed by the user:

- In M3, a call that matches no band (for example risk above 2.2 that is on task) goes
  to Ask.
- In M4, measure every configuration with and without the user request, to publish how
  much knowing the request adds.

## Milestone status

- **M0 Scaffold:** done. The live smoke call was answered by `jev-1.13.0` on the
  `typesafe` route on 8 Oct 2026.
- **M1 Passthrough proxy:** done. Through Tollgate, an MCP SDK client sees the same 15
  tools and the same results as it sees from the real filesystem and fetch servers
  directly. On 8 Oct 2026 Claude Code, pointed at Tollgate, called `read_text_file` on
  the filesystem upstream and `fetch` (raw mode) on the fetch upstream.
- **M2 Decision layer and audit:** built. In observe mode against the real upstreams and
  `jev-1.13.0`, five demo calls wrote five audit rows and none was blocked (9 Oct 2026).
  Jev latency from this machine over 30 warm calls: median 328 ms, p95 556 ms, slowest
  587 ms; a first call on a new connection took 488 ms. A four-question request used
  518 input tokens on average.
- M3 to M6: not started.
