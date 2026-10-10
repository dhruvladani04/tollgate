# Tollgate

Tollgate is an MCP proxy that checks every tool call an agent makes, and every tool result
coming back, with [Jev](https://docs.typesafe.ai/) before letting it through. It sits
between an MCP client (Claude Code, Cursor, a LangGraph agent) and the upstream MCP servers.
Each call ends one of three ways: allow, ask a human, or block.

**Status: early build.** Tollgate gates tool calls: it allows them, holds them for a person,
or blocks them. The gate has been measured on labelled calls; see
[How well it works](#how-well-it-works). The example policy still holds the build plan's
starting thresholds, not the tuned ones. A second model for uncertain calls and scanning of
tool results are still to come; see the milestones in [docs/spec.md](docs/spec.md).

Tollgate is not a security guarantee. Jev's vendor says adversarial content can move its
answers, so Tollgate is one layer of defense, not the only one.

## Set up

Tollgate needs [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 for you.

```text
uv sync
uv run pytest
```

The tests need no API key. A few of them start real server processes, so the run takes
about a minute.

## Give Tollgate a Jev key

Gating needs an API key for Jev, through either route:

| Route | Set these variables |
| --- | --- |
| TypeSafe directly (the default) | `TYPESAFE_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` and `TOLLGATE_JEV_ROUTE=openrouter` |

Put them in a file named `.env` in the repo root, one `NAME=value` per line. Git ignores
that file. Both routes pin Jev to version 1.13.0, so that tuned thresholds stay valid.

To check the key, send one request that asks a Noul, a Score and a Choice question:

```text
uv run --env-file .env python scripts/smoke_jev.py
```

## See it work

The demo starts Tollgate in front of two real MCP servers and plays both the client and the
person approving. It shows a call allowed, two blocked, one held until approved, one held
and denied, and one held because the model was given no time to answer:

```text
uv run --env-file .env python scripts/enforce_demo.py
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
| `enforce` | The default. The call is allowed, held for a person, or blocked. |
| `observe` | The same verdict is recorded, but every call is forwarded. Use it to see what enforcing would do before turning it on. |
| `passthrough` | The call is forwarded. Nothing is judged or recorded. Needs no key or policy. |

## Use Tollgate with Claude Code

Start Claude Code in the repo root with the example client config and the example settings:

```text
claude --mcp-config examples/claude_code.mcp.json --settings examples/claude_code.settings.json
```

`/mcp` should then show `tollgate` with 15 tools: 14 from the filesystem server and `fetch`.
Starting takes several seconds, because Tollgate starts each upstream before it answers.

[examples/claude_code.mcp.json](examples/claude_code.mcp.json) starts Tollgate with `.env`
and [policy.example.yaml](policy.example.yaml).
[examples/claude_code.settings.json](examples/claude_code.settings.json) sets three things:

- **It allows Tollgate's tools** (`mcp__tollgate__*`), so Claude Code does not ask for
  permission before each one. Without this you answer twice for one call: Claude Code's own
  prompt first, then Tollgate's.
- **It denies Claude Code's built-in `Edit` and `Write` tools.** Without this Claude Code
  usually changes files with its own tools, which never pass through Tollgate.
- **It runs `tollgate-hook` on every prompt,** which tells Tollgate what you asked for. The
  section "Tell Tollgate what the user asked for" below explains why that matters.

When Tollgate holds a call, Claude Code shows you the question and you accept or decline
there. `tollgate-approve`, described below, works from a second terminal as well.

Tollgate only sees calls to its own tools. Claude Code's built-in tools, including its
shell, are outside it, so a file can still be changed without Tollgate knowing. Tollgate
adds the most where the client has no built-in tool of its own, such as GitHub, a database
or email.

## How a call is judged

1. **Exact checks first.** A tool on the deny list, an argument that matches a secret
   pattern, or a path outside the allowed folders blocks the call. A tool on the allow list
   is let through. Arguments too large to judge are held. None of these reach the model, so
   a secret is never sent to it.
2. **Then the model.** Jev answers up to four questions about the call: how risky it is,
   whether it destroys data, whether it serves the user's request, and whether it sends data
   somewhere the request never named.
3. **Then the policy.** Code compares the answers with the thresholds in the policy file:

| Band | When | What Tollgate does |
| --- | --- | --- |
| Allow | Low risk, and no answer over its threshold | Forwards the call |
| Ask | Risky or destructive, or a smaller change that is not known to serve the user's request | Holds the call for a person |
| Block | Sends data where the request never named, or high risk and off task | Refuses the call and tells the agent why |
| Escalate | An answer that matters is uncertain | Holds the call for a person; a second model will judge these later |

**A call nobody asked for is held sooner.** The policy has two risk levels for asking.
`risk_ask` applies to every call. The lower one, `risk_ask_off_task`, applies when the
model judges the call off task, or when Tollgate was not told what the user asked for. So
an edit the user asked for can run, while the same edit made unprompted is held.

If the model does not answer in time, or fails, the call is held for a person. It is never
allowed by default.

Every number lives in the policy file, and a policy that leaves one out is rejected.
[policy.example.yaml](policy.example.yaml) explains each setting.

## How well it works

Measured on 10 October 2026 with `jev-1.13.0`, on 229 labelled tool calls that were kept
out of tuning:

| Configuration | Risky calls caught | Benign calls held | Malicious calls blocked |
| --- | --- | --- | --- |
| Exact checks only, no model | 11.9% | 1.1% | 13.8% |
| Jev at the starting thresholds | 97.0% | 28.7% | 56.9% |
| Jev at tuned thresholds | 93.3% | 7.4% | 87.9% |

- **Which thresholds ship.** The example policy holds the starting thresholds. The tuned
  ones are listed in the results and are not the default yet. On a second test set, derived
  from the InjecAgent benchmark, 62.8% of the calls they blocked were truly malicious,
  against 100% at the starting thresholds, and a blocked call cannot be approved by a
  person.
- **Without the user's request,** the tuned gate caught 95.6% of risky calls and held
  30.9% of benign ones. It cannot tell an edit the user asked for from one nobody asked
  for, so it holds both.
- **Speed and cost.** A decision took a median of 321 ms (416 ms at the 95th percentile)
  and costs about $0.022 per 1,000 calls.
- **Limits.** The test calls were written for this project and may be easier than real
  traffic. No case tries to talk the model into a wrong answer. The rule that holds
  unrequested calls sooner was added after seeing test results, so the tuned row is likely
  optimistic.

[docs/results.md](docs/results.md) has the full tables, a second test set derived from the
InjecAgent benchmark, calibration, stability and the method. It is generated from Jev's
answers, which are stored in the repo, so you can rebuild it with no API key:

```text
uv run python -m evals.report
```

## Approve or deny a held call

A held call waits until a person answers it. There are two ways to answer, and the first
answer wins:

- **In the client.** If the MCP client can ask its user a question (MCP calls this
  elicitation, and Claude Code supports it), Tollgate asks there. Accepting lets the call
  run; declining or dismissing refuses it.
- **From a terminal.** This always works, whatever the client:

```text
uv run tollgate-approve list
uv run tollgate-approve approve 3
uv run tollgate-approve deny 3
```

`list` shows each held call with its arguments and the reason it was held. A call that
nobody answers within the policy's `approval.timeout_seconds` is refused.

## See what was recorded

Every call Tollgate judges writes one row to `.tollgate/audit.sqlite`: the tool and its
arguments, the band and the reasons for it, what happened to the call, Jev's answers, the
model version, the time the decision took and the tokens it used. Secrets are redacted.

```text
uv run python -m tollgate.audit
```

Identical requests are answered from a cache in `.tollgate/jev-cache.jsonl`, so a repeated
call costs nothing and takes no time.

## Tell Tollgate what the user asked for

MCP sends a server the tool name and the arguments, and nothing about the conversation.
Two of Tollgate's four questions need the user's request: whether the call serves it, and
whether the call sends data somewhere the request never named. Without the request,
Tollgate asks only how risky the call is and whether it destroys data, so it can allow or
hold a call but has no grounds to block one. It also holds calls from the lower risk level,
because nothing shows that a call serves the user.

There are two ways to give Tollgate the request.

**From Claude Code, with a hook.** The example settings run `tollgate-hook` on every prompt.
It saves the prompt to `.tollgate/prompts.json`, and Tollgate uses the latest prompt as the
request and the two before it as recent context. Things to know:

- The latest prompt is not always the task. After a reply such as "yes, go ahead", the
  request is those words, and the task is only in the recent context.
- Prompts are matched to Tollgate by project folder. Two Claude Code sessions open in the
  same folder share one prompt file.
- Only prompts typed after Tollgate started are used, so a new session never inherits an
  old session's request.
- Your prompts are stored in that file and in the audit log, and they are sent to Jev with
  each call it judges. The policy's `context.max_chars` limits how much of each is sent.

**From any other client, in the call.** A client that knows the request passes it in the
`_meta` of each tool call. A request sent this way takes priority over the hook.

| `_meta` key | Value |
| --- | --- |
| `io.github.dhruvladani04.tollgate/user_request` | The user's request, as a string |
| `io.github.dhruvladani04.tollgate/recent_context` | Recent turns, as a list of strings; the last two are used |

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
