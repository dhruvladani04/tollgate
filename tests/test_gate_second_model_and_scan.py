"""The gate's second model for uncertain calls, and its scan of what tools return."""

import asyncio

import pytest
from fakes import FakeDecisionModel, FakeUpstream, call, decision, policy, refused, text_result

from tollgate.decide.base import Decision, DecisionError, NoulAnswer
from tollgate.proxy.intercept import Gate, ToolRouter

WRITE = call("write_file", path="notes.txt", content="new text")
HARMLESS = decision()
# A destructive answer of 0.5 has no confidence, so the engine escalates.
UNSURE = decision(destructive=0.5)
DESTRUCTIVE = decision(destructive=0.95)


def scan_answer(probability: float) -> Decision:
    return Decision(answers={"injected": NoulAnswer(probability=probability)}, model="jev-1.13.0")


class FirstModel:
    """Answers a question about a call with one decision and a result scan with another."""

    backend = "fake"
    model = "fake-1"

    def __init__(self, about_call, about_result=None) -> None:
        self.about_call = about_call
        self.about_result = about_result
        self.scanned: list[str] = []

    async def decide(self, request):
        if "tool_result" in request.state:
            self.scanned.append(request.state["tool_result"])
            outcome = self.about_result
        else:
            outcome = self.about_call
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Setup:
    def __init__(self, audit, approvals, first, second=None, *, enforce=True, **sections):
        self.upstream = FakeUpstream("write_file", "read_file")
        self.first = first
        self.second = second
        self.audit = audit
        self.approvals = approvals
        self.gate = Gate(
            ToolRouter({"files": self.upstream}),
            first,
            audit,
            policy(**sections),
            approvals,
            enforce=enforce,
            second_model=second,
        )

    def run(self, params):
        return asyncio.run(asyncio.wait_for(self.gate.call_tool(params), 10))

    def run_held(self, params):
        """Make a call that gets held, deny it, and return the held call and the result."""

        async def scenario():
            task = asyncio.create_task(self.gate.call_tool(params))
            while not self.approvals.pending():
                assert not task.done(), "the call finished without being held"
                await asyncio.sleep(0.01)
            [held] = self.approvals.pending()
            self.approvals.answer(held.id, approve=False)
            return held, await asyncio.wait_for(task, 10)

        return asyncio.run(scenario())

    @property
    def row(self):
        [row] = self.audit.recent(10)
        return row


# --- the second model ----------------------------------------------------------------


def test_an_uncertain_call_the_second_model_clears_runs_without_asking_anyone(audit, approvals):
    second = FakeDecisionModel(HARMLESS)
    setup = Setup(audit, approvals, FirstModel(UNSURE), second)

    result = setup.run(WRITE)

    assert result == text_result("write_file ran")
    assert approvals.pending() == []
    assert len(second.requests) == 1
    row = setup.row
    assert (row.band, row.action) == ("allow", "forwarded")
    assert row.escalation_model == "fake-1"
    assert row.escalation_answers == HARMLESS.answers
    assert row.escalation_latency_ms is not None
    # The first model's answers stay on record beside the second's.
    assert row.answers == UNSURE.answers
    assert any("second model" in reason for reason in row.reasons)


def test_the_second_model_is_sent_exactly_what_the_first_was(audit, approvals):
    first = FakeDecisionModel(UNSURE)
    second = FakeDecisionModel(HARMLESS)
    setup = Setup(audit, approvals, first, second)

    setup.run(WRITE)

    assert second.requests == first.requests


def test_a_call_the_first_model_is_sure_about_never_reaches_the_second(audit, approvals):
    second = FakeDecisionModel(HARMLESS)
    setup = Setup(audit, approvals, FirstModel(HARMLESS), second)

    setup.run(WRITE)

    assert second.requests == []
    assert setup.row.escalation_model is None


def test_the_second_model_can_send_the_call_to_a_person_or_block_it(audit, approvals):
    held, result = Setup(
        audit, approvals, FirstModel(UNSURE), FakeDecisionModel(DESTRUCTIVE)
    ).run_held(WRITE)

    assert "second model" in held.reasons[0] and "destructive" in held.reasons[0]
    assert "denied" in refused(result)


def test_a_block_from_the_second_model_refuses_the_call(audit, approvals):
    exfiltrating = decision(on_task=0.03, exfiltration=0.97)
    setup = Setup(audit, approvals, FirstModel(UNSURE), FakeDecisionModel(exfiltrating))

    result = setup.run(WRITE)

    assert "Blocked by Tollgate" in refused(result)
    assert setup.upstream.calls == []
    assert setup.row.band == "block"


@pytest.mark.parametrize(
    ("second", "expected"),
    [
        (FakeDecisionModel(UNSURE), "was uncertain too"),
        (FakeDecisionModel(DecisionError("rate_limited", "slow down")), "gave no answer"),
        (FakeDecisionModel(HARMLESS, delay=5), "gave no answer (timeout"),
    ],
    ids=["unsure-too", "failed", "too-slow"],
)
def test_a_second_model_that_does_not_settle_the_call_leaves_it_with_a_person(
    audit, approvals, second, expected
):
    setup = Setup(audit, approvals, FirstModel(UNSURE), second, escalation={"timeout_ms": 50})

    held, _ = setup.run_held(WRITE)

    assert any(expected in reason for reason in held.reasons)
    assert setup.upstream.calls == []
    assert setup.row.band == "escalate"


def test_without_a_second_model_an_uncertain_call_goes_straight_to_a_person(audit, approvals):
    setup = Setup(audit, approvals, FirstModel(UNSURE))

    held, _ = setup.run_held(WRITE)

    assert all("second model" not in reason for reason in held.reasons)


# --- scanning what a tool returns ----------------------------------------------------

SCAN_ON = {"enabled": True}


def test_a_clean_result_is_passed_on_unchanged_and_the_scan_is_recorded(audit, approvals):
    first = FirstModel(HARMLESS, scan_answer(0.04))
    setup = Setup(audit, approvals, first, result_scan=SCAN_ON)

    result = setup.run(WRITE)

    assert result == text_result("write_file ran")
    assert first.scanned == ["write_file ran"]
    row = setup.row
    assert (row.result_action, row.injected) == ("passed", 0.04)
    assert row.scan_latency_ms is not None
    assert row.withheld_result is None


def test_a_result_that_carries_instructions_is_withheld_and_kept_in_the_log(audit, approvals):
    setup = Setup(audit, approvals, FirstModel(HARMLESS, scan_answer(0.93)), result_scan=SCAN_ON)

    result = setup.run(WRITE)

    message = refused(result)
    assert "withheld the result of `write_file`" in message
    assert "write_file ran" not in message
    # The call itself ran: withholding the result does not undo it.
    assert setup.upstream.calls == [WRITE]
    row = setup.row
    assert (row.action, row.result_action, row.injected) == ("forwarded", "withheld", 0.93)
    assert row.withheld_result == "write_file ran"


def test_the_threshold_for_withholding_comes_from_the_policy(audit, approvals):
    lenient = {"enabled": True, "injected": 0.95}
    setup = Setup(audit, approvals, FirstModel(HARMLESS, scan_answer(0.93)), result_scan=lenient)

    assert setup.run(WRITE) == text_result("write_file ran")


@pytest.mark.parametrize(
    "failure",
    [DecisionError("timeout", "too slow"), Decision(answers={}, model="jev-1.13.0")],
    ids=["model-failed", "no-answer"],
)
def test_a_result_that_could_not_be_scanned_is_passed_on_with_a_warning(audit, approvals, failure):
    setup = Setup(audit, approvals, FirstModel(HARMLESS, failure), result_scan=SCAN_ON)

    result = setup.run(WRITE)

    assert not result.is_error
    assert "could not scan this result" in result.content[0].text
    assert result.content[1].text == "write_file ran"
    assert (setup.row.result_action, setup.row.injected) == ("unscanned", None)


def test_a_long_result_is_cut_before_it_is_scanned(audit, approvals):
    first = FirstModel(HARMLESS, scan_answer(0.02))
    setup = Setup(audit, approvals, first, result_scan={"enabled": True, "max_chars": 6})

    setup.run(WRITE)

    [scanned] = first.scanned
    assert scanned.startswith("wri") and scanned.endswith("ran")
    assert "left out" in scanned


def test_observe_mode_records_the_scan_and_withholds_nothing(audit, approvals):
    setup = Setup(
        audit,
        approvals,
        FirstModel(HARMLESS, scan_answer(0.93)),
        enforce=False,
        result_scan=SCAN_ON,
    )

    result = setup.run(WRITE)

    assert result == text_result("write_file ran")
    assert setup.row.result_action == "withheld"


def test_with_the_scan_switched_off_no_result_is_sent_to_the_model(audit, approvals):
    first = FirstModel(HARMLESS, scan_answer(0.93))
    setup = Setup(audit, approvals, first)

    assert setup.run(WRITE) == text_result("write_file ran")
    assert first.scanned == []
    assert setup.row.result_action is None


def test_a_refused_call_has_no_result_to_scan(audit, approvals):
    first = FirstModel(decision(on_task=0.03, exfiltration=0.97), scan_answer(0.93))
    setup = Setup(audit, approvals, first, result_scan=SCAN_ON)

    setup.run(WRITE)

    assert first.scanned == []
