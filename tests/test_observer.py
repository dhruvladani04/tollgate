"""Observe mode: every call gets a decision and an audit row, and nothing is blocked."""

import asyncio

import fake_upstream
import pytest
from fakes import DECISION, FakeDecisionModel, FakeUpstream, call, text_result
from mcp import Client

from tollgate.audit.store import AuditStore
from tollgate.decide.base import DecisionError
from tollgate.decide.cache import request_key
from tollgate.decide.questions import RECENT_CONTEXT_META, USER_REQUEST_META
from tollgate.proxy.intercept import DecisionObserver, ToolRouter, UnknownTool
from tollgate.proxy.server import build_server
from tollgate.proxy.upstreams import Upstream


@pytest.fixture
def audit(tmp_path):
    with AuditStore(tmp_path / "audit.sqlite") as store:
        yield store


def observer_over(upstream, model, audit) -> DecisionObserver:
    return DecisionObserver(ToolRouter({"files": upstream}), model, audit)


def test_a_call_is_forwarded_once_and_its_result_is_returned_unchanged(audit):
    files = FakeUpstream("write_file")
    observer = observer_over(files, FakeDecisionModel(), audit)
    params = call("write_file", path="notes.txt")

    result = asyncio.run(observer.call_tool(params))

    assert result == text_result("write_file ran")
    assert files.calls == [params]


def test_each_call_writes_one_row_with_the_decision_and_its_cost(audit):
    model = FakeDecisionModel()
    observer = observer_over(FakeUpstream("write_file"), model, audit)

    asyncio.run(observer.call_tool(call("write_file", path="notes.txt")))

    [row] = audit.recent(10)
    assert (row.mode, row.action, row.upstream, row.tool) == (
        "observe",
        "forwarded",
        "files",
        "write_file",
    )
    assert row.arguments == {"path": "notes.txt"}
    assert row.answers == DECISION.answers
    assert (row.backend, row.model) == ("fake", "jev-1.13.0")
    assert (row.input_tokens, row.output_tokens) == (431, 58)
    assert row.latency_ms is not None and row.latency_ms >= 0
    assert row.error is None
    assert row.request_key == request_key("fake", "fake-1", model.requests[0])


def test_the_model_is_asked_about_the_tool_with_its_description_and_arguments(audit):
    model = FakeDecisionModel()
    observer = observer_over(FakeUpstream("write_file"), model, audit)

    asyncio.run(observer.call_tool(call("write_file", path="notes.txt")))

    [request] = model.requests
    assert request.state == {
        "proposed_call": {
            "tool": "write_file",
            "description": "The write_file tool.",
            "arguments": {"path": "notes.txt"},
        }
    }


@pytest.mark.parametrize(
    ("failure", "recorded"),
    [
        (DecisionError("timeout", "Request timed out."), "timeout: Request timed out."),
        (DecisionError("rate_limited", "429 slow down"), "rate_limited: 429 slow down"),
        (RuntimeError("a bug"), "failed: a bug"),
    ],
    ids=["timeout", "rate-limit", "unexpected"],
)
def test_a_model_failure_is_recorded_and_the_call_still_goes_through(audit, failure, recorded):
    files = FakeUpstream("write_file")
    observer = observer_over(files, FakeDecisionModel(failure), audit)

    result = asyncio.run(observer.call_tool(call("write_file", path="notes.txt")))

    assert result == text_result("write_file ran")
    [row] = audit.recent(10)
    assert row.error == recorded
    assert row.answers == {}
    assert row.model is None


def test_a_broken_audit_log_does_not_stop_the_call(audit):
    files = FakeUpstream("write_file")
    observer = observer_over(files, FakeDecisionModel(), audit)
    audit.close()

    result = asyncio.run(observer.call_tool(call("write_file", path="notes.txt")))

    assert result == text_result("write_file ran")


def test_an_unknown_tool_is_refused_before_the_model_is_asked(audit):
    model = FakeDecisionModel()
    observer = observer_over(FakeUpstream("write_file"), model, audit)

    with pytest.raises(UnknownTool):
        asyncio.run(observer.call_tool(call("delete_everything")))

    assert model.requests == []
    assert audit.count() == 0


def test_context_sent_by_a_client_reaches_the_model_and_the_audit_row(audit):
    """Through the real MCP layers: client, Tollgate's server, observer, upstream."""
    model = FakeDecisionModel()
    meta = {
        USER_REQUEST_META: "Say hello back to me",
        RECENT_CONTEXT_META: ["turn one", "turn two", "turn three"],
    }

    async def scenario():
        async with Client(fake_upstream.build("text"), cache=None) as upstream:
            router = ToolRouter({"text": Upstream(upstream)})
            server = build_server(DecisionObserver(router, model, audit))
            async with Client(server, cache=None) as client:
                return await client.call_tool("echo", {"text": "hello"}, meta=meta)

    result = asyncio.run(scenario())

    assert result.content[0].text == "hello"
    [request] = model.requests
    assert request.state["user_request"] == "Say hello back to me"
    assert request.state["recent_context"] == ["turn two", "turn three"]
    assert set(request.questions) == {"risk", "destructive", "on_task", "exfiltration"}
    [row] = audit.recent(10)
    assert row.user_request == "Say hello back to me"
    assert row.tool == "echo"
