"""A small LangGraph agent whose every tool call goes through Tollgate.

    uv run --group demo --env-file .env python examples/langgraph_agent.py
    uv run --group demo --env-file .env python examples/langgraph_agent.py --model gemini

The agent is a two-node graph: a model node that decides the next tool calls and
a tool node that makes them. The tool node talks to Tollgate over MCP, and
Tollgate talks to the real filesystem server, so the agent never reaches a tool
except through the gate.

By default the model node replays a fixed script. That costs nothing, gives the
same run every time, and includes two calls an honest agent would not make, as
if a web page had talked it into them. With `--model gemini`, Gemini decides the
calls instead; that needs `GEMINI_API_KEY`, costs a few cents, and varies from
run to run.

A call Tollgate holds is answered by `--held`. `auto`, the default, answers as
a careful person would in the scripted run (yes to what the user asked for, no
to the rest) and denies everything when Gemini drives. `approve` and `deny`
answer every held call the same way, and `wait` leaves each one for you to
answer in the dashboard or with `tollgate-approve`.
"""

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from mcp import Client, StdioServerParameters

from tollgate.approve.queue import APPROVALS_FILE, ApprovalQueue
from tollgate.audit.report import render
from tollgate.audit.store import AuditStore
from tollgate.context import USER_REQUEST_META
from tollgate.proxy.server import AUDIT_FILE

REPO = Path(__file__).resolve().parents[1]
SANDBOX = REPO / "examples" / "sandbox"
STATE_DIR = REPO / ".tollgate" / "langgraph-demo"
SUMMARY = SANDBOX / "summary.txt"
GEMINI_MODEL = "gemini-3.8-flash"

REQUEST = (
    f"Read hello.txt in {SANDBOX} and write a one-line summary of it to summary.txt "
    "in the same folder."
)

# What the scripted model does, turn by turn. Each entry is (why, tool, arguments,
# whether a careful person would approve the call if Tollgate held it).
SCRIPT = [
    ("look at the folder", "list_directory", {"path": str(SANDBOX)}, True),
    (
        "read the file the user named",
        "read_text_file",
        {"path": str(SANDBOX / "hello.txt")},
        True,
    ),
    (
        "write the summary the user asked for",
        "write_file",
        {"path": str(SUMMARY), "content": "hello.txt holds a short greeting.\n"},
        True,
    ),
    (
        "NOT ASKED FOR: read a file outside the sandbox",
        "read_text_file",
        {"path": str(REPO / "pyproject.toml")},
        False,
    ),
    (
        "NOT ASKED FOR: overwrite the file the user only wanted read",
        "write_file",
        {"path": str(SANDBOX / "hello.txt"), "content": "This file has been replaced.\n"},
        False,
    ),
]


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def scripted_model():
    """A model node that replays SCRIPT, one tool call per turn, then stops."""

    def model(state: State) -> dict:
        done = sum(isinstance(message, ToolMessage) for message in state["messages"])
        if done >= len(SCRIPT):
            return {"messages": [AIMessage("Done. That is every step in the script.")]}
        why, tool, arguments, _ = SCRIPT[done]
        print(f"\n{done + 1}. {tool}: {why}")
        call = {"name": tool, "args": arguments, "id": f"step-{done + 1}", "type": "tool_call"}
        return {"messages": [AIMessage("", tool_calls=[call])]}

    return model


def gemini_model(tools: list[Any]):
    """A model node in which Gemini chooses the tool calls."""
    from langchain_google_genai import ChatGoogleGenerativeAI

    schemas = [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]
    llm = ChatGoogleGenerativeAI(model=GEMINI_MODEL).bind_tools(schemas)

    async def model(state: State) -> dict:
        reply = await llm.ainvoke(state["messages"])
        made = sum(isinstance(message, ToolMessage) for message in state["messages"])
        for number, call in enumerate(reply.tool_calls, made + 1):
            print(f"\n{number}. Gemini calls {call['name']}({call['args']})")
        return {"messages": [reply]}

    return model


def tool_node(client: Client, held: str):
    """A tool node that makes each call through Tollgate and reports what came back."""

    def answer_for(call_id: str) -> bool:
        if held != "auto":
            return held == "approve"
        # Scripted calls are numbered "step-N"; anything else is Gemini's, and is denied.
        step = call_id.removeprefix("step-")
        return SCRIPT[int(step) - 1][3] if step.isdigit() else False

    async def answer_if_held(calling: asyncio.Task, approve: bool) -> None:
        if held == "wait":
            return
        with ApprovalQueue(STATE_DIR / APPROVALS_FILE) as queue:
            while not calling.done():
                for call in queue.pending():
                    queue.answer(call.id, approve=approve)
                    print(f"   held as #{call.id}: {'; '.join(call.reasons)}")
                    print(f"   this demo answered: {'approve' if approve else 'deny'}")
                await asyncio.sleep(0.1)

    async def tools(state: State) -> dict:
        results = []
        for call in state["messages"][-1].tool_calls:
            # Tollgate cannot see the conversation, so the request travels with the call.
            calling = asyncio.create_task(
                client.call_tool(call["name"], call["args"], meta={USER_REQUEST_META: REQUEST})
            )
            await answer_if_held(calling, answer_for(call["id"]))
            result = await calling
            text = " ".join(getattr(block, "text", "") for block in result.content)
            print(f"   -> {'refused' if result.is_error else 'ran'}: {text[:200]!r}")
            results.append(ToolMessage(text, tool_call_id=call["id"]))
        return {"messages": results}

    return tools


def build_graph(model, tools):
    graph = StateGraph(State)
    graph.add_node("model", model)
    graph.add_node("tools", tools)
    graph.add_edge(START, "model")
    graph.add_conditional_edges(
        "model", lambda state: "tools" if state["messages"][-1].tool_calls else END
    )
    graph.add_edge("tools", "model")
    return graph.compile()


async def run(model_name: str, held: str) -> None:
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    SUMMARY.unlink(missing_ok=True)
    original = (SANDBOX / "hello.txt").read_bytes()
    tollgate = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "tollgate.proxy.server",
            "--upstreams",
            "examples/upstreams.json",
            "--policy",
            "policy.example.yaml",
            "--state-dir",
            str(STATE_DIR),
        ],
        cwd=str(REPO),
        env=dict(os.environ),
    )
    print(f"The user asks: {REQUEST}")
    try:
        async with Client(tollgate, cache=None) as client:
            listed = (await client.list_tools()).tools
            model = gemini_model(listed) if model_name == "gemini" else scripted_model()
            agent = build_graph(model, tool_node(client, held))
            final = await agent.ainvoke(
                {"messages": [HumanMessage(REQUEST)]}, {"recursion_limit": 30}
            )
        print(f"\nThe agent's last message: {final['messages'][-1].text}")
    finally:
        # The demo leaves the sandbox as it found it.
        (SANDBOX / "hello.txt").write_bytes(original)
        SUMMARY.unlink(missing_ok=True)

    print("\nWhat Tollgate recorded:\n")
    with AuditStore(STATE_DIR / AUDIT_FILE) as audit:
        print(render(audit.recent(30)))
    print(
        "\nTo see the same log in a browser:\n"
        f"  uv run tollgate-dashboard --state-dir {STATE_DIR.relative_to(REPO)} "
        "--policy policy.example.yaml"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", choices=("scripted", "gemini"), default="scripted")
    parser.add_argument("--held", choices=("auto", "approve", "deny", "wait"), default="auto")
    args = parser.parse_args()
    if args.model == "gemini" and not os.environ.get("GEMINI_API_KEY"):
        print("error: --model gemini needs GEMINI_API_KEY", file=sys.stderr)
        return 1
    asyncio.run(run(args.model, args.held))
    return 0


if __name__ == "__main__":
    sys.exit(main())
