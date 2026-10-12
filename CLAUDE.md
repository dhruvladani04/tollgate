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
uv run python -m evals.report                         rebuild docs/results.md from the cache
uv run python -m evals.tune                           print the fitted thresholds
uv run --env-file .env python -m evals.run            ask Jev about cases not yet cached
uv run python -m evals.build_golden                   rebuild the golden set from the scenes
uv run --env-file .env python -m evals.judge gemini-3.8-flash   ask an LLM judge (paid)
uv run --env-file .env python -m evals.judge gemma-4-31b        ask the free judge (slow)
uv run --env-file .env python -m evals.injections jev           ask Jev about the scan texts
uv run --env-file .env python scripts/scan_demo.py              M5 demo: result scan, second model
uv run tollgate-dashboard --policy policy.example.yaml          the local dashboard, port 8787
uv run tollgate-init --dir somewhere                            write a starting policy and upstreams
uv build                                                        build the tollgate-mcp wheel into dist/
uv run --group demo --env-file .env python examples/langgraph_agent.py   M6 demo agent
claude --mcp-config examples/claude_code.mcp.json --settings examples/claude_code.settings.json
```

`.env` sets `TOLLGATE_JEV_ROUTE` and the key for that route. A few tests start real
server processes and one rebuilds the evaluation report, so the full run takes about a
minute.

While a Claude Code session is using Tollgate, Windows locks
`.venv/Scripts/tollgate.exe`, and any `uv run` or `uv add` that reinstalls the project
fails. Add `--no-sync` to `uv run`, or call `.venv/Scripts/python.exe` directly. Do not
stop the user's session to free the file.

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

## The LLM judge

- **Same interface, same questions.** `decide/llm.py` puts a `DecisionRequest` to an LLM
  through TypeSafe's `system-one-adapter`, so the judge has no prompt of its own.
- **The gate's second model comes from the policy,** not from `JUDGES`: provider
  (`gemini`, `anthropic` or `openai`), model id and thinking level. The user asked for
  this so that someone with a budget can use a stronger model. Only the Gemini SDK is
  installed; the other two are optional extras, and a missing one stops startup with
  the install command. `JUDGES` holds the two judges the evaluation used.
- **Each judge is pinned** in `JUDGES`: model id, thinking level and corrective retries.
  The thinking level is part of the judge's name, and so of its cache key. Gemini 3.8
  Flash rejects thinking `minimal`; `low` is the least it accepts.
- **Keys:** `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, by provider. The
  names live in `decide/base.py`, and `proxy/upstreams.py` withholds all three from
  upstream servers.
- **Thinking tokens are billed as output** but reported apart, and the adapter's own
  Gemini provider leaves them out. `_GeminiJudgeProvider` adds them, so recorded output
  tokens match the bill.
- **It is slow.** A Gemini 3.8 Flash answer takes about 4 seconds, five times the gate's
  800 ms budget, so escalation in the gate needs its own time budget in the policy.
- **Limits on the user's account (11 Oct 2026):** Gemini 3.8 Flash is on the paid Tier 1
  key; its free tier allows about 20 requests a day. Gemma 4 31B is free at 30 requests
  and 16,000 tokens a minute. OpenRouter's free models allow 50 requests a day on this
  account and are not used. The user's budget for Gemini is one to two dollars; the
  full run of 782 cases cost about one.

## The policy and the gate

- **Every number is in the policy file.** `policy/schema.py` gives no section a default,
  so a policy that leaves out a threshold is rejected.
- **What the example policy ships.** `policy.example.yaml` holds the two ask levels M4
  fitted (`risk_ask` 1.4, `risk_ask_off_task` 0.2) and the spec's starting value for every
  other threshold. The report calls this "Jev, ask levels tuned", and a test fails if the
  file and that configuration differ. The evaluation's baseline thresholds are
  `START_THRESHOLDS` in `evals/data.py`; they are not read from the example policy.
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
  3. Ask: risk at or above `risk_ask`; or risk at or above `risk_ask_off_task` on a call
     that is confidently off task or came with no user request; or `destructive` above
     its threshold.
  4. Allow: everything else.
- **The off-task rule is not in the spec's band table.** It was added on 10 Oct 2026;
  `docs/decisions/0001-off-task-ask-rule.md` says why. Setting `risk_ask_off_task` equal
  to `risk_ask` switches it off, and the schema rejects a value above `risk_ask`.
- **`on_task` matters for a high-risk call and for a call between the two ask levels,** so
  its uncertainty is ignored otherwise. An off-task call below `risk_ask_off_task` is
  allowed.
- **Without a user request** the block rules cannot fire, a high-risk call is held, and
  the lower ask level applies to every call.
- **Escalate is held like Ask,** unless the policy's `escalation.enabled` is on. Then
  the call is first put to the second model the policy names, within
  `escalation.timeout_ms`, with the same request and the same thresholds. A clear answer
  from it decides the band. If it is uncertain too, fails or is late, the call stays
  Escalate and a person is asked. A second model can also block.
- **The result scan** (`Gate._scan`) runs after a call is forwarded. It asks Jev the
  `injected` question about the result's text, cut to `result_scan.max_chars`. Over
  `result_scan.injected` the result is withheld and its text kept in the audit row; a
  failed or late scan passes the result on behind a warning; observe mode withholds
  nothing. The audit row is written before the call runs and annotated afterwards.
  Only text blocks are scanned.
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

## The dashboard and the demo agent

- **Two parts.** `src/tollgate/dashboard/app.py` is a FastAPI JSON API under `/api`. The
  pages are a Next.js app (React, TypeScript, Tailwind) in the repo's `dashboard/`
  folder. The user rejected server-rendered HTML templates as unprofessional (12 Oct
  2026), so do not go back to them.
- **The pages are a static export, and the build is committed.** `npm run build` in
  `dashboard/` exports to `out/` and copies it to `src/tollgate/dashboard/static/`,
  which `tollgate-dashboard` serves beside the API. Running needs no Node. After any
  change under `dashboard/`, run the build and commit `static/` with it.
- **Read `dashboard/AGENTS.md` first.** This Next.js version (16) differs from older
  ones and ships its own docs under `dashboard/node_modules/next/dist/docs/`.
- **In development,** `npm run dev` serves the pages on port 3000 and rewrites `/api` to
  `tollgate-dashboard` on 8787. Rewrites do not exist in a static export, so the config
  only sets `output: export` outside development.
- **The API reads files, not the proxy.** It opens the audit log and the approval queue
  per request and closes them again, so it works with the proxy stopped and never
  holds a lock.
- **It can approve calls, so it guards them.** Answering a held call needs the token
  from `/api/session` in an `X-Tollgate-Token` header. Another site can make the
  browser send a request to localhost, but cannot read that token or add the header.
  Keep the check, keep answering a POST and never a GET, and never add CORS headers.
- **It listens on 127.0.0.1 and has no login.** Changing `--host` exposes the approve
  buttons to whoever can reach it.
- **Tool arguments, user requests and withheld results are attacker-controlled text.**
  The pages render them through React as text. Never use `dangerouslySetInnerHTML`.
- **Look at it before calling it done.** Headless Chrome can screenshot a running
  dashboard: `chrome --headless=new --window-size=1360,900 --virtual-time-budget=8000
  --screenshot=out.png http://127.0.0.1:8787/`. The README's three screenshots in
  `docs/img/dashboard-*.png` were taken this way, in light mode.
- **`langchain-mcp-adapters` cannot be used.** It requires version 1 of the `mcp` SDK and
  installing it downgrades the SDK Tollgate is built on. The demo agent wraps the MCP
  client by hand instead. After any `uv pip install`, check `mcp` is still 2.x.
- **The demo agent** (`examples/langgraph_agent.py`) has a scripted model node by default
  and Gemini with `--model gemini`. Its packages are in the optional `demo` group, so
  its tests skip when they are not installed. It restores the sandbox file byte for
  byte, because a text-mode rewrite changes line endings on Windows.

## The evaluation

- **Scenes, not cases, are the source.** `evals/scenes/*.py` holds 330 hand-written
  scenes. `evals/golden.py` pairs each with a request it serves and an unrelated one,
  and derives every label from the scene's facts by fixed rules. Change a label by
  changing a fact or a rule, never one case.
- **The data files are committed.** After editing a scene, run
  `python -m evals.build_golden`; a test fails if `evals/data/golden_calls.jsonl`
  differs from what the scenes build. A changed call has a new cache key, so run
  `evals.run` to fill it, then `evals.report`.
- **Both cases of a scene share a split,** fixed by `SPLIT_SEED`. The InjecAgent-derived
  set is test only and is never used for tuning.
- **The cache is the evaluation.** `evals/cache/` holds Jev's answers keyed by exact
  request, plus the timed repeats. `evals.report` reads only that, calls no model and
  needs no key. A test fails if `docs/results.md` differs from what the cache produces,
  so rerun the report after any change to the data, tuning or report code.
- **A case that carries a secret is never sent to Jev.** `evals.run` skips it exactly as
  the gate would, and a test checks that the cache holds no answer for one. The golden
  file contains AWS's published example key id (`AKIA...EXAMPLE`) for those cases; it
  is not a real credential.
- **Tuning** (`evals/tune.py`) has two stages: each yes/no threshold is fitted to its
  own label by F1, then `risk_ask`, `risk_ask_off_task` and `confidence` are chosen
  together by running the real band engine over the train split. Only settings that
  catch at least as many risky calls as the starting policy are eligible; among them the
  fewest benign holds wins. An earlier F2 objective was dropped because it reached 100%
  recall by holding half the benign calls.
- **A narrower fit, `tune(..., ask_levels_only=True)`,** fits only `risk_ask` and
  `risk_ask_off_task` and leaves every other threshold at its starting value, so the gate
  blocks exactly what it blocked before. The report lists it as "Jev, ask levels tuned".
- **The test split has informed one design choice.** The off-task rule was added after
  reading where the gate was wrong on the test split, and the report says so. Do not add
  another rule or change the tuning that way without saying so in the report.
- **Known weak spots, from the report:** the tuned gate allows most off-task reads of
  secrets or personal data, because `risk` scores a read as read-only; tuned block
  precision on the InjecAgent set is 62.8%; and the tuned `confidence` of 0.05 makes
  bands less stable across repeats (91.2% agreement, against 98.2% at 0.4).
- **Judge answers are cached per judge** in `evals/cache/judge-<name>.jsonl`, with call
  times and failures in files beside it. `evals/judges.py` computes the two judge
  configurations from those: the judge on every call, and Jev with only its uncertain
  calls put to the judge. The judges were asked with the user request only, once each,
  with no stability repeats.
- **The README's headline numbers are copied from `docs/results.md`.** Update them when
  the report changes.
- **Figures** follow one two-colour palette on a light surface, set at the top of
  `evals/report.py`.

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
  dashboard/          the dashboard's JSON API and its built pages (M6)
  context.py          call context types; import-light for the hook
  prompts.py          the Claude Code hook command and its prompt log
docs/results.md       the evaluation report, generated; do not edit by hand
dashboard/            the dashboard's pages: a Next.js app, built into the package
evals/
  scenes/             the hand-written scenes, one file per tool family
  golden.py           labelling rules and the train/test split
  injecagent.py       builds the external test set from InjecAgent
  data/               the committed case files
  cache/              the committed model answers and timed repeats
  run.py              asks Jev (needs a key); tune.py, report.py read the cache
  metrics.py          pure metric functions
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

Built in M4 on Claude's judgment, and not explicitly confirmed by the user:

- Every configuration is measured with and without the user request.
- The labelling rules in `evals/golden.py`. The one that moves the numbers most: an
  easy-to-undo change the user asked for counts as benign.
- The tuning objective described under "The evaluation".
- The external set uses InjecAgent only. AgentDojo was not adapted.
- The tool-result data (deepset/prompt-injections) waits for M5, which builds the
  result scan it measures.

- **Off-task ask rule (decided 10 Oct 2026).** The user chose to add it to the band
  engine after M4's first results.
- **The example policy ships only the two tuned ask levels (decided 10 Oct 2026).** The
  user first chose to ship no tuned threshold, then, after the mix was measured, to ship
  these two. The thresholds behind a block stay at their starting values, because fully
  tuned block precision on the external set is too low and a wrong block cannot be
  approved by a person. Ask before changing them.
- **`httpx2` is a declared dependency, and the original plan file and
  `jev_in_agent_loop.svg` are gitignored (decided 10 Oct 2026).**

Built with the off-task rule on Claude's judgment, and not explicitly confirmed:

- A call with no user request is treated like an off-task one for this rule. Measured
  on the train split at the tuned thresholds, such clients catch 95.8% of risky calls
  this way and 81.2% if the rule cannot fire without a request.
- An uncertain `on_task` answer between the two ask levels escalates.
- The starting value of `risk_ask_off_task` is 0.5.

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
- **M4 Eval and tuning:** built on 10 Oct 2026. `python -m evals.report` rebuilds
  `docs/results.md` and three figures from the committed cache in about 15 seconds with
  no key. The data is 581 golden cases (352 train, 229 test) and 205 InjecAgent-derived
  test cases. Numbers below include the off-task rule, added the same day. On the golden
  test split, rules alone caught 11.9% of risky calls; Jev at the starting thresholds
  caught 97.0% and held 28.7% of benign calls; tuned, 93.3% and 7.4%, with malicious
  calls blocked outright rising from 56.9% to 87.9%. Tuned the same way with the rule
  off, 93.3% and 30.9%. With only the two ask levels tuned, which is what the example
  policy ships, 96.3% and 9.6%, and the same blocks as at the start; on the InjecAgent
  set that configuration is no better than the start (87.8% and 18.4%, against 89.1% and
  16.3%), and for a client that sends no user request it doubles benign holds there
  (8.2% to 16.3%). Median decision time 321 ms, p95 416 ms; about $0.022 per 1,000
  calls. Two of the plan's five configurations need the LLM judge and arrive with M5.
- **M5 Escalation and result scan:** built on 11 Oct 2026. The report covers all five
  configurations. On the golden test split with the shipped thresholds: Jev alone caught
  96.3% of risky calls and asked a person about 41.5% of calls; Jev with Gemini 3.8 Flash
  on uncertain calls, 91.1% and 31.0%; Gemini on every call, 91.1% and 31.4% at about 60
  times the cost and 12 times the time. Because escalation catches fewer risky calls, the
  user chose to ship it off, with the second model set in the policy. The result scan is
  on in the example policy: on deepset's 116 test prompts Jev's AUROC is 0.987, and at
  the shipped 0.5 it withheld 73.3% of injections with no false alarm. The live demo
  (`scripts/scan_demo.py`) passed a clean page, withheld an injected one at 0.98, and
  held a call both models were unsure about.
- **Decided with the user for M5 (11 Oct 2026):** escalation off by default; the second
  model chosen in the policy; an injected result is withheld, not passed with a warning;
  an unscannable result is passed with a warning.
- **The scan threshold stays at 0.5 (decided with the user, 11 Oct 2026).** The value
  fitted on the train prompts is 0.10 (88.3% recall and 98.1% precision on test, against
  73.3% and 100% at 0.5), but those are chatbot prompts, and nothing measures how often
  0.10 would withhold a real page. `docs/decisions/0002-result-scan-threshold.md` has
  the record. Measuring the scan on real tool output is what would justify a change.
- **M6 Dashboard and write-up:** built on 12 Oct 2026. The dashboard is a Next.js app
  over a FastAPI JSON API, with the three views the plan names; it was checked in a
  browser, in light and dark, against the demos' real audit log. The LangGraph agent
  ran through Tollgate in both modes: scripted, where the out-of-sandbox read was
  blocked and the unrequested overwrite held; and with Gemini choosing the calls. The
  README opens with the headline comparison, a five-minute start, and what Tollgate is
  not. Decided with the user: the agent has both a scripted and a Gemini driver, and
  the dashboard and demo packages were approved.
- **Going public (started 12 Oct 2026).** Done: an MIT `LICENSE` (the user said to add a
  licence and did not name one; MIT was Claude's choice, so confirm it before the repo is
  public), `CONTRIBUTING.md`, package metadata, and a history check: 12 commits, one
  author identity, no keys, no personal paths. `uv build` gives a wheel that includes the
  dashboard's pages; installed into a clean environment, its commands run from outside
  the repo.
- **The package is `tollgate-mcp`; the import is still `tollgate`** (decided with the
  user, 12 Oct 2026). `tollgate` is taken on PyPI by an unrelated project in the same
  space, ravi-labs/tollgate. `[tool.uv.build-backend] module-name` maps the two, and
  `importlib.metadata.version` must be asked for `tollgate-mcp`.
- **Not published.** Never publish to PyPI or change the repo's visibility without the
  user saying so. Until it is published, the README tells people to install from the
  repository; change that line when the first release goes out.
- **`tollgate-init`** (`src/tollgate/init.py`) writes `policy.yaml` and `upstreams.json`
  for someone with no copy of the repo. It ships `src/tollgate/examples/policy.example.yaml`,
  which must stay identical to the root `policy.example.yaml`: a test fails if they
  differ, so copy the file across whenever the example policy changes.
- **Public since 12 Oct 2026,** under MIT, which the user confirmed.
- **A fresh clone was checked on this machine (12 Oct 2026):** `uv sync` and the tests
  passed (395, with the demo agent's tests skipped because its packages are optional),
  `evals.report` reproduced the report, and installing straight from GitHub gave working
  commands and dashboard pages. It has still not been run on macOS or Linux.
- **`.gitattributes` keeps `docs/results.md` and `*.jsonl` on Unix line endings.** Without
  it, regenerating the report on Windows showed the file as changed when only its line
  endings differed.
