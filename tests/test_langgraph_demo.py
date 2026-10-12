"""The LangGraph demo's graph, run against a stand-in for Tollgate.

The demo's packages are an optional install, so these tests are skipped without them.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("langgraph")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "langgraph_agent.py"
spec = importlib.util.spec_from_file_location("langgraph_agent", EXAMPLE)
demo = importlib.util.module_from_spec(spec)
sys.modules["langgraph_agent"] = demo
spec.loader.exec_module(demo)

from langchain_core.messages import HumanMessage, ToolMessage  # noqa: E402


class FakeTollgate:
    """Stands in for the MCP client: refuses the given tools and runs the rest."""

    def __init__(self, refuse: set[str]) -> None:
        self.refuse = refuse
        self.calls = []

    async def call_tool(self, name, arguments, meta=None):
        self.calls.append((name, arguments, meta))
        refused = name in self.refuse
        text = "Blocked by Tollgate" if refused else f"{name} ran"
        return SimpleNamespace(is_error=refused, content=[SimpleNamespace(text=text)])


def run_scripted(client) -> list:
    agent = demo.build_graph(demo.scripted_model(), demo.tool_node(client, held="wait"))
    final = asyncio.run(agent.ainvoke({"messages": [HumanMessage(demo.REQUEST)]}))
    return final["messages"]


def test_the_scripted_agent_makes_every_call_through_the_gate_in_order():
    client = FakeTollgate(refuse=set())

    messages = run_scripted(client)

    assert [name for name, _, _ in client.calls] == [tool for _, tool, _, _ in demo.SCRIPT]
    assert [arguments for _, arguments, _ in client.calls] == [
        arguments for _, _, arguments, _ in demo.SCRIPT
    ]
    assert sum(isinstance(message, ToolMessage) for message in messages) == len(demo.SCRIPT)


def test_every_call_carries_the_users_request_for_the_gate_to_judge_against():
    client = FakeTollgate(refuse=set())

    run_scripted(client)

    assert all(meta == {demo.USER_REQUEST_META: demo.REQUEST} for _, _, meta in client.calls)


def test_a_refusal_reaches_the_agent_as_the_tools_answer_and_the_run_goes_on():
    client = FakeTollgate(refuse={"write_file"})

    messages = run_scripted(client)

    answers = [message.content for message in messages if isinstance(message, ToolMessage)]
    assert answers.count("Blocked by Tollgate") == 2
    assert len(client.calls) == len(demo.SCRIPT)


def test_the_script_marks_which_held_calls_a_careful_person_would_approve():
    approve = {why: answer for why, _, _, answer in demo.SCRIPT}

    assert all(not answer for why, answer in approve.items() if why.startswith("NOT ASKED FOR"))
    assert any(approve.values())
