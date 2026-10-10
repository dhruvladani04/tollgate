"""The gate: every call is judged and recorded; when enforcing, it is allowed, held or blocked."""

import asyncio

import fake_upstream
import pytest
from fakes import (
    DECISION,
    FakeDecisionModel,
    Scene,
    call,
    decision,
    policy,
    refused,
    text_result,
)
from mcp import Client

from tollgate.approve.queue import ApprovalStatus
from tollgate.context import RECENT_CONTEXT_META, USER_REQUEST_META
from tollgate.decide.base import DecisionError
from tollgate.decide.cache import request_key
from tollgate.proxy.intercept import Gate, ToolRouter, UnknownTool
from tollgate.proxy.server import build_server
from tollgate.proxy.upstreams import Upstream

AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
WRITE = call("write_file", path="notes.txt", content="new text")
HARMLESS = decision()
DESTRUCTIVE = decision(destructive=0.91)


# --- allow ---------------------------------------------------------------------------


def test_a_harmless_call_is_forwarded_at_once_and_its_result_returned_unchanged(audit, approvals):
    scene = Scene(audit, approvals, HARMLESS)

    result = scene.run(WRITE)

    assert result == text_result("write_file ran")
    assert scene.upstream.calls == [WRITE]
    assert approvals.pending() == []
    assert (scene.row.mode, scene.row.band, scene.row.action) == ("enforce", "allow", "forwarded")


def test_each_call_writes_one_row_with_the_decision_and_its_cost(audit, approvals):
    scene = Scene(audit, approvals, DECISION, enforce=False)

    scene.run(WRITE)

    row = scene.row
    assert (row.upstream, row.tool) == ("files", "write_file")
    assert row.arguments == {"path": "notes.txt", "content": "new text"}
    assert row.answers == DECISION.answers
    assert (row.backend, row.model) == ("fake", "jev-1.13.0")
    assert (row.input_tokens, row.output_tokens) == (431, 58)
    assert row.latency_ms is not None and row.latency_ms >= 0
    assert row.error is None
    assert row.request_key == request_key("fake", "fake-1", scene.model.requests[0])


# --- ask: held until a person answers -------------------------------------------------


def test_a_destructive_call_is_held_until_it_is_approved(audit, approvals):
    scene = Scene(audit, approvals, DESTRUCTIVE)

    held, forwarded_while_held, result = scene.run_held(WRITE, answer=True)

    assert forwarded_while_held == []
    assert (held.tool, held.upstream) == ("write_file", "files")
    assert held.arguments == {"path": "notes.txt", "content": "new text"}
    assert any("destructive" in reason for reason in held.reasons)
    assert result == text_result("write_file ran")
    assert scene.upstream.calls == [WRITE]
    assert (scene.row.band, scene.row.action, scene.row.approval_id) == (
        "ask",
        "forwarded",
        held.id,
    )


def test_a_held_call_that_is_denied_is_never_run(audit, approvals):
    scene = Scene(audit, approvals, DESTRUCTIVE)

    held, _, result = scene.run_held(WRITE, answer=False)

    assert scene.upstream.calls == []
    assert f"denied this call through Tollgate (held call #{held.id})" in refused(result)
    assert "destructive" in refused(result)
    assert (scene.row.band, scene.row.action) == ("ask", "denied")


def test_a_held_call_nobody_answers_is_refused_when_its_time_runs_out(audit, approvals):
    scene = Scene(audit, approvals, DESTRUCTIVE, rules=policy(approval={"timeout_seconds": 1}))

    held, _, result = scene.run_held(WRITE, answer=None)

    assert scene.upstream.calls == []
    assert "nobody approved it within 1 seconds" in refused(result)
    assert approvals.get(held.id).status is ApprovalStatus.EXPIRED
    assert scene.row.action == "expired"


def test_an_uncertain_answer_is_held_for_a_person_until_escalation_exists(audit, approvals):
    scene = Scene(audit, approvals, decision(destructive=0.47))

    held, forwarded_while_held, _ = scene.run_held(WRITE, answer=True)

    assert forwarded_while_held == []
    assert any("uncertain" in reason for reason in held.reasons)
    assert (scene.row.band, scene.row.action) == ("escalate", "forwarded")


def test_a_held_call_the_client_gives_up_on_is_marked_cancelled(audit, approvals):
    scene = Scene(audit, approvals, DESTRUCTIVE)

    async def scenario():
        task = asyncio.create_task(scene.gate.call_tool(WRITE))
        while not approvals.pending():
            await asyncio.sleep(0.01)
        [held] = approvals.pending()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return held.id

    call_id = asyncio.run(scenario())

    assert approvals.get(call_id).status is ApprovalStatus.CANCELLED
    assert scene.upstream.calls == []


# --- fail closed: no decision means a person decides ----------------------------------


@pytest.mark.parametrize(
    ("outcome", "delay", "recorded"),
    [
        (DecisionError("timeout", "Request timed out."), 0.0, "timeout: Request timed out."),
        (DecisionError("rate_limited", "429 slow down"), 0.0, "rate_limited: 429 slow down"),
        (DecisionError("unreachable", "no route to host"), 0.0, "unreachable: no route to host"),
        (DecisionError("failed", "Jev did not answer: risk"), 0.0, "failed: Jev did not answer"),
        (RuntimeError("a bug"), 0.0, "failed: a bug"),
        (HARMLESS, 5.0, "timeout: no decision within 50 ms"),
    ],
    ids=["timeout", "rate-limit", "unreachable", "incomplete", "unexpected", "over-budget"],
)
def test_when_the_model_gives_no_decision_the_call_is_held_never_allowed(
    audit, approvals, outcome, delay, recorded
):
    scene = Scene(audit, approvals, outcome, delay=delay, rules=policy(decision={"timeout_ms": 50}))

    held, forwarded_while_held, result = scene.run_held(WRITE, answer=False)

    assert forwarded_while_held == []
    assert scene.upstream.calls == []
    assert any("no decision from the model" in reason for reason in held.reasons)
    assert result.is_error
    row = scene.row
    assert (row.band, row.action) == ("ask", "denied")
    assert row.error.startswith(recorded)
    assert row.answers == {}


def test_a_call_held_for_lack_of_a_decision_still_runs_once_a_person_approves(audit, approvals):
    scene = Scene(audit, approvals, DecisionError("timeout", "Request timed out."))

    _, _, result = scene.run_held(WRITE, answer=True)

    assert result == text_result("write_file ran")
    assert scene.upstream.calls == [WRITE]


# --- block ----------------------------------------------------------------------------


def test_an_exfiltrating_call_is_blocked_without_asking_anyone(audit, approvals):
    scene = Scene(audit, approvals, decision(risk=1.5, on_task=0.03, exfiltration=0.93))

    result = scene.run(WRITE)

    assert scene.upstream.calls == []
    assert approvals.pending() == []
    assert refused(result).startswith("Blocked by Tollgate: exfiltration")
    assert (scene.row.band, scene.row.action) == ("block", "blocked")


# --- exact checks never reach the model -----------------------------------------------


def test_a_denied_tool_is_blocked_and_the_model_is_not_asked(audit, approvals):
    scene = Scene(
        audit, approvals, HARMLESS, rules=policy(prefilter={"deny_tools": ["delete_everything"]})
    )

    result = scene.run(call("delete_everything"))

    assert "deny list" in refused(result)
    assert scene.model.requests == []
    assert scene.upstream.calls == []
    row = scene.row
    assert (row.band, row.action, row.backend, row.latency_ms) == ("block", "blocked", None, None)


def test_a_secret_is_blocked_before_it_can_reach_the_model_and_is_kept_out_of_the_log(
    audit, approvals
):
    scene = Scene(audit, approvals, HARMLESS)

    result = scene.run(call("write_file", path="notes.txt", content=f"key = {AWS_KEY}"))

    assert "looks like a secret (aws_access_key_id)" in refused(result)
    assert scene.model.requests == []
    assert scene.upstream.calls == []
    assert scene.row.arguments == {
        "path": "notes.txt",
        "content": "key = [redacted: aws_access_key_id]",
    }


def test_a_path_outside_the_sandbox_is_blocked_without_asking_the_model(audit, approvals, tmp_path):
    rules = policy(prefilter={"path_sandbox": {"roots": ["sandbox"], "arguments": ["path"]}})
    scene = Scene(audit, approvals, HARMLESS, rules=rules, cwd=tmp_path)

    result = scene.run(call("read_file", path="../id_rsa"))

    assert "outside the allowed folders" in refused(result)
    assert scene.model.requests == []
    assert scene.upstream.calls == []


def test_an_allowed_tool_is_forwarded_without_asking_the_model(audit, approvals):
    scene = Scene(
        audit, approvals, DESTRUCTIVE, rules=policy(prefilter={"allow_tools": ["read_file"]})
    )

    result = scene.run(call("read_file", path="notes.txt"))

    assert result == text_result("read_file ran")
    assert scene.model.requests == []
    assert (scene.row.band, scene.row.action) == ("allow", "forwarded")


def test_arguments_too_large_for_the_model_are_held_without_asking_it(audit, approvals):
    scene = Scene(audit, approvals, HARMLESS, rules=policy(prefilter={"max_argument_bytes": 100}))

    held, _, result = scene.run_held(call("write_file", content="x" * 500), answer=True)

    assert scene.model.requests == []
    assert any("100-byte limit" in reason for reason in held.reasons)
    assert result == text_result("write_file ran")


# --- policy ---------------------------------------------------------------------------


def test_a_tool_override_changes_the_band_for_that_tool_only(audit, approvals):
    rules = policy(tools={"write_file": {"thresholds": {"destructive": 0.05}}})
    scene = Scene(audit, approvals, decision(destructive=0.1), rules=rules)

    scene.run(call("read_file", path="notes.txt"))
    held, _, _ = scene.run_held(WRITE, answer=True)

    assert held.tool == "write_file"
    assert [row.band for row in audit.recent(10)] == ["ask", "allow"]


def test_the_model_is_asked_about_the_tool_with_its_description_and_arguments(audit, approvals):
    scene = Scene(audit, approvals, HARMLESS)

    scene.run(WRITE)

    [request] = scene.model.requests
    assert request.state == {
        "proposed_call": {
            "tool": "write_file",
            "description": "The write_file tool.",
            "arguments": {"path": "notes.txt", "content": "new text"},
        }
    }


def test_an_unknown_tool_is_refused_before_anything_is_judged(audit, approvals):
    scene = Scene(audit, approvals, HARMLESS)

    with pytest.raises(UnknownTool):
        scene.run(call("no_such_tool"))

    assert scene.model.requests == []
    assert audit.count() == 0


def test_an_enforcing_gate_refuses_a_call_it_cannot_record(audit, approvals):
    scene = Scene(audit, approvals, HARMLESS)
    audit.close()

    result = scene.run(WRITE)

    assert "could not record this call" in refused(result)
    assert scene.upstream.calls == []


# --- observe mode: the same verdicts, recorded but never acted on -----------------------


@pytest.mark.parametrize(
    ("outcome", "band"),
    [
        (HARMLESS, "allow"),
        (DESTRUCTIVE, "ask"),
        (decision(risk=1.5, on_task=0.03, exfiltration=0.93), "block"),
        (decision(destructive=0.47), "escalate"),
        (DecisionError("timeout", "Request timed out."), "ask"),
    ],
    ids=["allow", "ask", "block", "escalate", "no-decision"],
)
def test_observe_mode_records_the_band_and_forwards_the_call_anyway(audit, outcome, band):
    scene = Scene(audit, None, outcome, enforce=False)

    result = scene.run(WRITE)

    assert result == text_result("write_file ran")
    assert scene.upstream.calls == [WRITE]
    assert (scene.row.mode, scene.row.band, scene.row.action) == ("observe", band, "forwarded")


def test_observe_mode_still_keeps_a_secret_from_the_model(audit):
    scene = Scene(audit, None, HARMLESS, enforce=False)

    result = scene.run(call("write_file", content=f"key = {AWS_KEY}"))

    assert result == text_result("write_file ran")
    assert scene.model.requests == []
    assert (scene.row.band, scene.row.action) == ("block", "forwarded")
    assert AWS_KEY not in str(scene.row.arguments)


def test_observe_mode_forwards_even_when_the_audit_log_is_broken(audit):
    scene = Scene(audit, None, HARMLESS, enforce=False)
    audit.close()

    assert scene.run(WRITE) == text_result("write_file ran")


def test_enforcing_without_an_approval_queue_is_a_programming_error(audit):
    with pytest.raises(ValueError, match="approval queue"):
        Scene(audit, None, HARMLESS, enforce=True)


# --- through the real MCP layers ------------------------------------------------------


def test_context_sent_by_a_client_reaches_the_model_and_the_audit_row(audit):
    model = FakeDecisionModel(decision(on_task=0.97, exfiltration=0.02))
    meta = {
        USER_REQUEST_META: "Say hello back to me",
        RECENT_CONTEXT_META: ["turn one", "turn two", "turn three"],
    }

    async def scenario():
        async with Client(fake_upstream.build("text"), cache=None) as upstream:
            router = ToolRouter({"text": Upstream(upstream)})
            gate = Gate(router, model, audit, policy(), enforce=False)
            async with Client(build_server(gate), cache=None) as client:
                return await client.call_tool("echo", {"text": "hello"}, meta=meta)

    result = asyncio.run(scenario())

    assert result.content[0].text == "hello"
    [request] = model.requests
    assert request.state["user_request"] == "Say hello back to me"
    assert request.state["recent_context"] == ["turn two", "turn three"]
    assert set(request.questions) == {"risk", "destructive", "on_task", "exfiltration"}
    [row] = audit.recent(10)
    assert (row.user_request, row.tool, row.band) == ("Say hello back to me", "echo", "allow")


def test_a_blocked_call_reaches_the_client_as_a_tool_error_naming_the_reason(audit, approvals):
    model = FakeDecisionModel(decision(risk=1.5, on_task=0.03, exfiltration=0.93))
    meta = {USER_REQUEST_META: "Say hello back to me"}

    async def scenario():
        async with Client(fake_upstream.build("text"), cache=None) as upstream:
            router = ToolRouter({"text": Upstream(upstream)})
            gate = Gate(router, model, audit, policy(), approvals, enforce=True)
            async with Client(build_server(gate), cache=None) as client:
                return await client.call_tool("echo", {"text": "hello"}, meta=meta)

    result = asyncio.run(scenario())

    assert result.is_error
    assert result.content[0].text.startswith("Blocked by Tollgate: exfiltration")
