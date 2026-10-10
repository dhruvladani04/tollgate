"""The gate asking the client's user about a held call, and learning the user's request."""

import asyncio

import fake_upstream
import pytest
from fakes import FakeDecisionModel, Scene, call, decision, policy, refused, text_result
from mcp import Client, types

from tollgate.approve.prompt import APPROVAL_KEY
from tollgate.approve.queue import ApprovalStatus
from tollgate.context import USER_REQUEST_META, CallContext
from tollgate.proxy.intercept import ClientChannel, Gate, ToolRouter
from tollgate.proxy.server import build_server
from tollgate.proxy.upstreams import Upstream

AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
WRITE = call("write_file", path="notes.txt", content="new text")
HARMLESS = decision()
DESTRUCTIVE = decision(destructive=0.91)
ROUND_TRIP = ClientChannel(round_trip=True)


class FakeUser:
    """The person at the client: answers the gate's question, or fails to be reachable."""

    def __init__(self, action="accept", error=None, delay=0.0):
        self.action = action
        self.error = error
        self.delay = delay
        self.questions: list[str] = []
        self.schemas: list[dict] = []

    async def __call__(self, message, schema):
        self.questions.append(message)
        self.schemas.append(schema)
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return types.ElicitResult(action=self.action)


def answering(first: types.InputRequiredResult, params, action="accept", state=None):
    """The call a client makes again after its user answered the gate's question."""
    responses = {} if action is None else {APPROVAL_KEY: types.ElicitResult(action=action)}
    return params.model_copy(
        update={
            "request_state": first.request_state if state is None else state,
            "input_responses": responses,
        }
    )


# --- the user's request, from the Claude Code hook --------------------------------------


def test_a_request_recorded_by_the_hook_is_used_when_the_call_carries_none(audit, approvals):
    hooked = CallContext(user_request="Tidy my notes", recent_context=("Open the notes folder",))
    scene = Scene(audit, approvals, HARMLESS, ambient=lambda: hooked)

    scene.run(WRITE)

    [request] = scene.model.requests
    assert request.state["user_request"] == "Tidy my notes"
    assert request.state["recent_context"] == ["Open the notes folder"]
    assert set(request.questions) == {"risk", "destructive", "on_task", "exfiltration"}
    assert scene.row.user_request == "Tidy my notes"


def test_a_request_sent_with_the_call_wins_over_the_hook(audit, approvals):
    scene = Scene(audit, approvals, HARMLESS, ambient=lambda: CallContext(user_request="Hooked"))
    explicit = WRITE.model_copy(update={"meta": {USER_REQUEST_META: "Sent with the call"}})

    scene.run(explicit)

    assert scene.model.requests[0].state["user_request"] == "Sent with the call"


def test_with_no_request_from_either_source_only_the_call_is_judged(audit, approvals):
    scene = Scene(audit, approvals, HARMLESS, ambient=lambda: None)

    scene.run(WRITE)

    [request] = scene.model.requests
    assert "user_request" not in request.state
    assert set(request.questions) == {"risk", "destructive"}


def test_a_long_request_is_cut_to_the_policys_length_keeping_both_ends(audit, approvals):
    long_prompt = "Summarise this log. " + "x" * 5000 + " Then save it."
    scene = Scene(
        audit,
        approvals,
        HARMLESS,
        rules=policy(context={"max_chars": 60}),
        ambient=lambda: CallContext(user_request=long_prompt, recent_context=("y" * 500,)),
    )

    scene.run(WRITE)

    state = scene.model.requests[0].state
    assert state["user_request"].startswith("Summarise this log.")
    assert state["user_request"].endswith("Then save it.")
    assert "characters left out" in state["user_request"]
    assert len(state["user_request"]) < 120
    assert len(state["recent_context"][0]) < 120


# --- asking now and waiting (protocols before 2026-07-28) -------------------------------


def test_the_clients_user_can_approve_a_held_call_without_the_command(audit, approvals):
    user = FakeUser("accept")
    scene = Scene(audit, approvals, DESTRUCTIVE)

    result = scene.run(WRITE, ClientChannel(elicit=user))

    assert result == text_result("write_file ran")
    assert scene.upstream.calls == [WRITE]
    assert approvals.get(1).status is ApprovalStatus.APPROVED
    assert (scene.row.band, scene.row.action, scene.row.approval_id) == ("ask", "forwarded", 1)


def test_the_question_names_the_tool_its_arguments_and_the_reason(audit, approvals):
    user = FakeUser("accept")
    scene = Scene(audit, approvals, DESTRUCTIVE)

    scene.run(WRITE, ClientChannel(elicit=user))

    [question] = user.questions
    assert "held call #1" in question
    assert "Tool: write_file (from files)" in question
    assert '"path": "notes.txt"' in question
    assert "destructive" in question
    assert user.schemas == [{"type": "object", "properties": {}}]


@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_declining_or_dismissing_the_question_refuses_the_call(audit, approvals, action):
    scene = Scene(audit, approvals, DESTRUCTIVE)

    result = scene.run(WRITE, ClientChannel(elicit=FakeUser(action)))

    assert "denied this call" in refused(result)
    assert scene.upstream.calls == []
    assert approvals.get(1).status is ApprovalStatus.DENIED
    assert scene.row.action == "denied"


def test_when_the_client_cannot_be_asked_the_command_still_can_answer(audit, approvals):
    user = FakeUser(error=RuntimeError("the client has no elicitation"))
    scene = Scene(audit, approvals, DESTRUCTIVE)

    async def scenario():
        task = asyncio.create_task(scene.gate.call_tool(WRITE, ClientChannel(elicit=user)))
        while not user.questions:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        still_held = not task.done()
        approvals.answer(1, approve=True)
        return still_held, await asyncio.wait_for(task, 10)

    still_held, result = asyncio.run(scenario())

    assert still_held
    assert result == text_result("write_file ran")


def test_an_answer_from_the_command_ends_the_wait_while_the_client_is_still_asking(
    audit, approvals
):
    user = FakeUser("decline", delay=30)
    scene = Scene(audit, approvals, DESTRUCTIVE)

    async def scenario():
        task = asyncio.create_task(scene.gate.call_tool(WRITE, ClientChannel(elicit=user)))
        while not user.questions:
            await asyncio.sleep(0.01)
        approvals.answer(1, approve=True)
        return await asyncio.wait_for(task, 10)

    result = asyncio.run(scenario())

    assert result == text_result("write_file ran")
    assert approvals.get(1).status is ApprovalStatus.APPROVED


def test_a_call_blocked_for_holding_a_secret_asks_no_one(audit, approvals):
    user = FakeUser("accept")
    scene = Scene(audit, approvals, HARMLESS)

    result = scene.run(call("write_file", content=f"key = {AWS_KEY}"), ClientChannel(elicit=user))

    assert "looks like a secret" in refused(result)
    assert user.questions == []


def test_a_blocked_call_does_not_ask_anyone(audit, approvals):
    user = FakeUser("accept")
    scene = Scene(audit, approvals, decision(risk=1.5, on_task=0.03, exfiltration=0.93))

    result = scene.run(WRITE, ClientChannel(elicit=user))

    assert refused(result).startswith("Blocked by Tollgate")
    assert user.questions == []


# --- asking by round trip (the 2026-07-28 protocol) -------------------------------------


def test_a_held_call_is_answered_with_a_question_and_nothing_runs(audit, approvals):
    scene = Scene(audit, approvals, DESTRUCTIVE)

    first = scene.run(WRITE, ROUND_TRIP)

    assert isinstance(first, types.InputRequiredResult)
    [(key, question)] = first.input_requests.items()
    assert key == APPROVAL_KEY
    assert isinstance(question, types.ElicitRequest)
    assert "Tool: write_file (from files)" in question.params.message
    assert question.params.requested_schema == {"type": "object", "properties": {}}
    assert first.request_state
    assert scene.upstream.calls == []
    assert approvals.get(1).status is ApprovalStatus.PENDING
    assert audit.count() == 0


def test_calling_again_with_an_acceptance_runs_the_call_once(audit, approvals):
    scene = Scene(audit, approvals, DESTRUCTIVE)
    first = scene.run(WRITE, ROUND_TRIP)

    result = scene.run(answering(first, WRITE), ROUND_TRIP)

    assert result == text_result("write_file ran")
    assert scene.upstream.calls == [WRITE]
    assert len(scene.model.requests) == 1
    assert approvals.get(1).status is ApprovalStatus.APPROVED
    assert (scene.row.band, scene.row.action, scene.row.approval_id) == ("ask", "forwarded", 1)


def test_the_upstream_never_sees_the_gates_own_question_and_answer(audit, approvals):
    scene = Scene(audit, approvals, DESTRUCTIVE)
    first = scene.run(WRITE, ROUND_TRIP)

    scene.run(answering(first, WRITE), ROUND_TRIP)

    [forwarded] = scene.upstream.calls
    assert forwarded.request_state is None
    assert forwarded.input_responses is None


@pytest.mark.parametrize("action", ["decline", "cancel", None])
def test_calling_again_without_an_acceptance_refuses_the_call(audit, approvals, action):
    scene = Scene(audit, approvals, DESTRUCTIVE)
    first = scene.run(WRITE, ROUND_TRIP)

    result = scene.run(answering(first, WRITE, action), ROUND_TRIP)

    assert "denied this call" in refused(result)
    assert scene.upstream.calls == []
    assert scene.row.action == "denied"


def test_an_answer_given_through_the_command_first_is_the_one_that_counts(audit, approvals):
    scene = Scene(audit, approvals, DESTRUCTIVE)
    first = scene.run(WRITE, ROUND_TRIP)
    approvals.answer(1, approve=True)

    result = scene.run(answering(first, WRITE, "decline"), ROUND_TRIP)

    assert result == text_result("write_file ran")


@pytest.mark.parametrize(
    "tamper",
    [
        lambda state: state[:-1] + ("0" if state[-1] != "0" else "1"),
        lambda state: state.replace(".1.", ".2.", 1),
        lambda state: "tollgate.approval.garbage",
        lambda state: "some-upstream-state",
    ],
    ids=["bad-signature", "another-call-id", "malformed", "not-ours"],
)
def test_a_forged_or_foreign_state_is_not_an_approval(audit, approvals, tamper):
    """The call is judged afresh and held again; it does not run."""
    scene = Scene(audit, approvals, DESTRUCTIVE)
    first = scene.run(WRITE, ROUND_TRIP)

    again = scene.run(answering(first, WRITE, state=tamper(first.request_state)), ROUND_TRIP)

    assert isinstance(again, types.InputRequiredResult)
    assert scene.upstream.calls == []


def test_an_approval_cannot_be_reused_for_different_arguments(audit, approvals):
    scene = Scene(audit, approvals, DESTRUCTIVE)
    first = scene.run(WRITE, ROUND_TRIP)
    other = call("write_file", path="notes.txt", content="something else entirely")

    again = scene.run(answering(first, other), ROUND_TRIP)

    assert isinstance(again, types.InputRequiredResult)
    assert scene.upstream.calls == []


def test_an_approval_cannot_be_used_twice(audit, approvals):
    scene = Scene(audit, approvals, DESTRUCTIVE)
    first = scene.run(WRITE, ROUND_TRIP)
    scene.run(answering(first, WRITE), ROUND_TRIP)

    again = scene.run(answering(first, WRITE), ROUND_TRIP)

    assert isinstance(again, types.InputRequiredResult)
    assert scene.upstream.calls == [WRITE]


# --- through the real MCP layers, with a real client answering --------------------------


class Person:
    """An MCP client's elicitation callback: the person answering the gate's question."""

    def __init__(self, action: str) -> None:
        self.action = action
        self.messages: list[str] = []

    async def __call__(self, context, params):
        self.messages.append(params.message)
        return types.ElicitResult(action=self.action)


def through_mcp(audit, approvals, person, mode):
    """Call a tool through a real client and Tollgate's real server, both in this process."""
    model = FakeDecisionModel(DESTRUCTIVE)

    async def scenario():
        async with Client(fake_upstream.build("text"), cache=None) as upstream:
            router = ToolRouter({"text": Upstream(upstream)})
            gate = Gate(router, model, audit, policy(), approvals, enforce=True)
            client = Client(build_server(gate), cache=None, mode=mode, elicitation_callback=person)
            async with client:
                return await client.call_tool("echo", {"text": "hello"})

    return asyncio.run(asyncio.wait_for(scenario(), 30))


@pytest.mark.parametrize("mode", ["legacy", "auto"], ids=["older-protocol", "2026-07-28"])
def test_a_real_client_is_asked_and_its_acceptance_lets_the_call_run(audit, approvals, mode):
    person = Person("accept")

    result = through_mcp(audit, approvals, person, mode)

    assert not result.is_error
    assert result.content[0].text == "hello"
    [message] = person.messages
    assert "Tool: echo (from text)" in message
    assert approvals.get(1).status is ApprovalStatus.APPROVED
    [row] = audit.recent(10)
    assert (row.band, row.action) == ("ask", "forwarded")


@pytest.mark.parametrize("mode", ["legacy", "auto"], ids=["older-protocol", "2026-07-28"])
def test_a_real_client_declining_refuses_the_call(audit, approvals, mode):
    result = through_mcp(audit, approvals, Person("decline"), mode)

    assert result.is_error
    assert "denied this call" in result.content[0].text
    [row] = audit.recent(10)
    assert row.action == "denied"


def test_a_client_that_cannot_ask_its_user_leaves_the_call_to_the_command(audit, approvals):
    model = FakeDecisionModel(DESTRUCTIVE)

    async def scenario():
        async with Client(fake_upstream.build("text"), cache=None) as upstream:
            router = ToolRouter({"text": Upstream(upstream)})
            gate = Gate(router, model, audit, policy(), approvals, enforce=True)
            async with Client(build_server(gate), cache=None) as client:
                calling = asyncio.create_task(client.call_tool("echo", {"text": "hello"}))
                while not approvals.pending():
                    assert not calling.done(), "the call finished without being held"
                    await asyncio.sleep(0.01)
                approvals.answer(1, approve=True)
                return await calling

    result = asyncio.run(asyncio.wait_for(scenario(), 30))

    assert result.content[0].text == "hello"
