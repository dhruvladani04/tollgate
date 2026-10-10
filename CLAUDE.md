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
uv run pytest                         run the tests (no key needed)
uv run ruff check .                   lint
uv run ruff format .                  format
uv run --env-file .env python scripts/smoke_jev.py    one live Jev call
uv run --env-file .env python scripts/observe_demo.py         M2 demo: calls plus audit log
uv run --env-file .env python scripts/enforce_demo.py         M3 demo: allow, block, hold
uv run --env-file .env python scripts/measure_jev_latency.py  p50 and p95 of Jev from here
uv run python -m tollgate.audit                       print the latest audit records
uv run tollgate-approve list                          show the calls being held
uv run tollgate-approve approve 3                     or: deny 3
uv run --env-file .env tollgate --upstreams examples/upstreams.json --policy policy.example.yaml
claude --mcp-config examples/claude_code.mcp.json --settings examples/claude_code.settings.json
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
- **Tollgate only sees calls to its own tools.** Claude Code's built-in tools (Edit, Write,
  the shell) never pass through an MCP server, and the model prefers them unless they are
  disallowed. Claude Code also asks its own permission before each MCP call unless
  `mcp__tollgate__*` is allowed, which shows up as a second prompt before Tollgate's hold.
  The README's Claude Code section has the start command that avoids both.
- **stdout is the protocol.** Log to stderr only.
- **Only tools are forwarded so far.** Resources, prompts and upstream notifications are not.
- **Modes.** `--mode enforce` is the default: each call is allowed, held or blocked.
  `--mode observe` records the same verdict but forwards every call. `--mode passthrough`
  forwards only and needs no key or policy.

## The decision layer

- **One interface.** `DecisionModel` in `decide/base.py` takes a `DecisionRequest`, which
  is a System One request: a `state` plus named questions as plain wire-shape dicts. Any
  backend that answers that shape can stand behind the gate, which is how the LLM judge
  arrives in M5.
- **Questions.** `decide/questions.py` holds the spec's wording. Jev judges each Score
  level on its own, without seeing the other levels, so each level describes a whole
  situation. Read the Jev docs (Score, Noul, jaggedness) before rewording anything.
- **The user's request is not in MCP.** A tool call carries a tool name and arguments,
  nothing about the conversation. The gate looks for the request in two places, in this
  order: the call's `_meta` (keys in `context.py`), then the prompts the Claude Code hook
  recorded (`prompts.py`, file `.tollgate/prompts.json`). Without either, only `risk` and
  `destructive` are asked, because `on_task` and `exfiltration` need `user_request`.
- **The hook's rules.** The latest prompt is the request and the two before it are recent
  context. Only prompts recorded after this Tollgate process started count, a new session
  id starts the list again, and a prompt inside a subagent is ignored. The hook prints
  nothing on stdout (Claude Code would show it to the model) and never exits with status
  2 (that would block the user's prompt).
- **Keep `context.py` free of heavy imports.** The hook loads it on every prompt. Importing
  the MCP SDK there cost 1.2 seconds; without it the import takes 0.06 seconds.
- **The request and each turn are cut** to the policy's `context.max_chars`, keeping the
  start and the end.
- **Arguments go to Jev whole,** up to the policy's `max_argument_bytes`. Past that, the
  pre-filter holds the call for a person and the model is not asked.
- **Cache.** `decide/cache.py` keys on the exact request and stores JSON lines, so an
  evaluation can commit its cache. Jev's answers to the same request vary by a few
  hundredths between calls; a cached answer does not.
- **Audit.** `audit/store.py` writes one SQLite row per call. It uses SQLite's default
  journal mode. With write-ahead logging, opening the file while Tollgate was shutting
  down failed once on Windows with a disk I/O error; the cause was not confirmed, so
  test on Windows before turning it back on.
- **Observe mode blocks nothing.** A model failure or an audit failure is recorded or
  logged, and the call is still forwarded.

## The policy and the gate

- **Every number is in the policy file.** `policy/schema.py` gives no section a default,
  so a policy that leaves out a threshold is rejected. `policy.example.yaml` holds the
  spec's starting values, which are untuned until M4.
- **`Gate` in `proxy/intercept.py` is the whole flow:** pre-filter, then the model within
  the time budget, then the band engine, then the action, then one audit row.
- **Pre-filter order** (`policy/prefilter.py`): deny list, secret patterns, path sandbox,
  allow list, argument size. The first match decides and the model is never asked. A
  secret therefore never reaches Jev, and it is redacted in the audit log and in the
  approval queue.
- **Band engine** (`policy/engine.py`), first match wins:
  1. Block: a confident `exfiltration`, or a confident risk above `risk_block` on a call
     that is confidently off task.
  2. Escalate: an answer that could change the band is uncertain, or `on_task` and
     `exfiltration` contradict each other.
  3. Ask: risk at or above `risk_ask`, or `destructive` above its threshold.
  4. Allow: everything else.
- **`on_task` only matters for a high-risk call,** so its uncertainty is ignored otherwise.
  A low-risk off-task call is allowed, as the spec's table says.
- **Without a user request** the block rules cannot fire, and a high-risk call is held.
- **Escalate is held like Ask** until M5 adds the LLM judge.
- **Fail closed.** No decision within `decision.timeout_ms`, or any model error, holds the
  call for a person. An enforcing gate also refuses a call whose audit row it cannot write.
- **Approval.** A held call always goes into `.tollgate/approvals.sqlite`, where
  `tollgate-approve` can answer it. If the client declared the elicitation capability,
  the gate also asks the client's user, and the first answer wins. After
  `approval.timeout_seconds` with no answer, the call is refused.
- **Two ways to ask the client,** chosen per call by `client_channel` in
  `proxy/server.py`. Before the 2026-07-28 protocol the gate sends an elicitation
  request and waits. On 2026-07-28 servers may not send requests, so the gate answers the
  call with an input-required result and the client calls again with the answer. That
  second call carries a state string the gate signed and bound to the tool and
  arguments; a forged, foreign or reused state is treated as a new call.
- **Known gap:** if an upstream itself answers a gated call with an input-required
  result, the client's retry is judged again and may be held a second time.
- **Refusals are tool results with `isError`,** not protocol errors, so the agent can read
  the reason.
- **A cached decision survives an outage:** the cache answers before the model is asked.

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
  context.py          call context types; import-light for the hook
  prompts.py          the Claude Code hook command and its prompt log
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

Built on Claude's proposal, and not explicitly confirmed by the user:

- A call that matches no band in the spec's table (for example risk above 2.2 that is on
  task) goes to Ask.
- The exact outcome of each pre-filter check: deny list, secrets and path sandbox block;
  oversized arguments are held; the allow list skips the model.
- Uncertainty escalates only when it could change the band, and a confident block signal
  wins over an unrelated uncertain answer.

- **Hook and in-client approval, built in one step (decided 9 Oct 2026).** The user chose
  to build the Claude Code hook and approval through MCP elicitation together.

Proposed by Claude and not yet confirmed by the user:

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
- **M3 Policy and approval:** built. In the live demo on 9 Oct 2026 a destructive
  overwrite was held until `tollgate-approve` approved it, and with the model given 1 ms
  to answer a call was held, not allowed. Exact checks blocked an out-of-sandbox path
  and an access key. About one in five first calls on a new connection took longer than
  the 800 ms budget in earlier runs, so some calls will be held for that reason alone.
- **Hook and in-client approval (after M3):** built on 9 Oct 2026. Tests drive both ways
  of asking the client with the MCP SDK's own client over stdio, and run the hook command
  against a live Tollgate. Confirmed inside Claude Code on 10 Oct 2026: with the example
  settings, a `write_file` call got no Claude Code permission prompt, the hook supplied
  the user's prompt so all four questions were answered, and the hold was approved from
  the in-client question in 4 seconds.
- M4 to M6: not started.
