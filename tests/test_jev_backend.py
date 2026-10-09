import asyncio
import json

import httpx2
import pytest
from typesafe_sdk import RetryPolicy

from tollgate.decide.base import Decision, DecisionError, DecisionRequest, NoulAnswer, ScoreAnswer
from tollgate.decide.jev import ROUTES, JevDecisionModel, make_client

REQUEST = DecisionRequest(
    state={"proposed_call": {"tool": "git_push", "arguments": {"force": True}}},
    questions={
        "risk": {"type": "score", "instructions": "How risky?", "criteria": ["low", "high"]},
        "destructive": {"type": "noul", "instructions": "`proposed_call` deletes data"},
    },
)
# Shaped like the response body in the TypeSafe API reference.
RESPONSE = {
    "model": "jev-1.13.0",
    "answers": {
        "risk": {
            "type": "score",
            "score": 0.7,
            "confidence": 0.4,
            "legend": {"0": "low", "1": "high"},
            "probabilities": {"1": 0.7, "0": 0.3},
        },
        "destructive": {"type": "noul", "noul": 0.87},
    },
    "usage": {"input_tokens": 431, "output_tokens": 58},
}


def ask(handler, route_name="typesafe") -> Decision:
    """Put REQUEST to a Jev backend whose HTTP calls go to `handler`."""
    route = ROUTES[route_name]

    async def scenario():
        client = make_client(
            route,
            env={route.api_key_env: "test-key"},
            transport=httpx2.MockTransport(handler),
            retry=RetryPolicy(max_retries=0),
        )
        async with client:
            return await JevDecisionModel(client, route.model).decide(REQUEST)

    return asyncio.run(scenario())


def test_answers_tokens_and_the_answering_model_are_read_from_the_response():
    decision = ask(lambda request: httpx2.Response(200, json=RESPONSE))

    assert decision == Decision(
        answers={
            "risk": ScoreAnswer(score=0.7, probabilities=(0.3, 0.7), confidence=0.4),
            "destructive": NoulAnswer(probability=0.87),
        },
        model="jev-1.13.0",
        input_tokens=431,
        output_tokens=58,
    )


@pytest.mark.parametrize(
    ("route_name", "pinned"),
    [("typesafe", "jev-1.13.0"), ("openrouter", "typesafe/jev-1.13")],
)
def test_the_request_carries_the_state_the_questions_and_the_pinned_model(route_name, pinned):
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=RESPONSE)

    ask(handler, route_name)

    assert seen == [
        {"state": dict(REQUEST.state), "questions": dict(REQUEST.questions), "model": pinned}
    ]


@pytest.mark.parametrize(
    ("status", "kind"),
    [(429, "rate_limited"), (500, "failed"), (401, "failed"), (422, "failed")],
)
def test_an_error_status_becomes_a_decision_error_of_the_right_kind(status, kind):
    with pytest.raises(DecisionError) as caught:
        ask(lambda request: httpx2.Response(status, json={"error": "nope"}))

    assert caught.value.kind == kind


def test_a_timeout_and_a_dead_connection_are_told_apart():
    def too_slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("too slow", request=request)

    def refused(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(DecisionError) as timed_out:
        ask(too_slow)
    with pytest.raises(DecisionError) as unreachable:
        ask(refused)

    assert timed_out.value.kind == "timeout"
    assert unreachable.value.kind == "unreachable"


def test_a_response_that_skips_a_question_is_a_failure_not_a_partial_decision():
    partial = RESPONSE | {"answers": {"destructive": RESPONSE["answers"]["destructive"]}}

    with pytest.raises(DecisionError, match="did not answer: risk") as caught:
        ask(lambda request: httpx2.Response(200, json=partial))

    assert caught.value.kind == "failed"


def test_an_answer_type_the_gate_does_not_use_is_a_failure():
    choice = {"type": "choice", "choice": "a", "confidence": 1.0, "probabilities": {"a": 1.0}}
    odd = RESPONSE | {"answers": RESPONSE["answers"] | {"risk": choice}}

    with pytest.raises(DecisionError, match="unsupported choice answer"):
        ask(lambda request: httpx2.Response(200, json=odd))
