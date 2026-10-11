"""The LLM judge, driven through the adapter with a provider that never touches the network."""

import asyncio
import json

import pytest
import typesafe_sdk as sdk
from system_one_adapter.providers.base import ProviderResult

from tollgate.decide.base import DecisionError, DecisionRequest, NoulAnswer, ScoreAnswer
from tollgate.decide.llm import (
    JUDGES,
    Judge,
    LlmConfigError,
    LlmDecisionModel,
    make_provider,
    read_api_key,
    resolve_judge,
)

REQUEST = DecisionRequest(
    state={"proposed_call": {"tool": "git_push", "arguments": {"force": True}}},
    questions={
        "risk": {"type": "score", "instructions": "How risky?", "criteria": ["low", "high"]},
        "destructive": {"type": "noul", "instructions": "`proposed_call` deletes data"},
    },
)
GOOD = json.dumps({"answers": {"risk": {"0": 0.25, "1": 0.75}, "destructive": 0.9}})
JUDGE = Judge("test-model", "low", corrective_retries=1)


class FakeProvider:
    """Replies with each given text in turn, or raises it, and records what it was sent."""

    model_name = "test-model"

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.requests = []

    async def request(self, messages, *, schema, structured):
        self.requests.append((messages, schema, structured))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ProviderResult(text=reply, input_tokens=200, output_tokens=90)

    def translate_error(self, error):
        return error


def ask(*replies, judge: Judge = JUDGE):
    provider = FakeProvider(*replies)

    async def scenario():
        model = LlmDecisionModel(judge, provider, retry=sdk.RetryPolicy(max_retries=0))
        try:
            return await model.decide(REQUEST)
        finally:
            await model.aclose()

    return provider, scenario


def test_the_judge_answers_in_the_same_shape_as_jev():
    provider, scenario = ask(GOOD)

    decision = asyncio.run(scenario())

    assert decision.answers["destructive"] == NoulAnswer(probability=0.9)
    risk = decision.answers["risk"]
    assert isinstance(risk, ScoreAnswer)
    assert risk.score == pytest.approx(0.75)
    assert risk.probabilities == pytest.approx((0.25, 0.75))
    assert decision.model == "test-model/thinking-low"
    assert (decision.input_tokens, decision.output_tokens) == (200, 90)


def test_the_judge_is_sent_the_state_and_asked_for_structured_output():
    provider, scenario = ask(GOOD)

    asyncio.run(scenario())

    [(messages, schema, structured)] = provider.requests
    assert structured is True
    assert "git_push" in messages[-1].content
    assert set(schema["$defs"]["TypeSafeAnswers"]["properties"]) == {"risk", "destructive"}


def test_a_malformed_answer_is_sent_back_once_and_every_attempt_is_counted():
    provider, scenario = ask("{}", GOOD)

    decision = asyncio.run(scenario())

    assert len(provider.requests) == 2
    assert decision.answers["destructive"] == NoulAnswer(probability=0.9)
    assert (decision.input_tokens, decision.output_tokens) == (400, 180)


def test_an_answer_that_stays_malformed_is_a_failed_decision_not_a_guess():
    _, scenario = ask("{}", "{}")

    with pytest.raises(DecisionError) as failure:
        asyncio.run(scenario())

    assert failure.value.kind == "failed"


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (sdk.TypeSafeAPITimeoutError("too slow"), "timeout"),
        (sdk.TypeSafeAPIConnectionError("refused"), "unreachable"),
    ],
)
def test_provider_failures_become_decision_errors_the_gate_understands(error, kind):
    _, scenario = ask(error)

    with pytest.raises(DecisionError) as failure:
        asyncio.run(scenario())

    assert failure.value.kind == kind


def test_each_judge_is_pinned_to_one_model_and_thinking_level():
    flash = resolve_judge("gemini-3.8-flash")

    assert (flash.model, flash.thinking) == ("gemini-3.8-flash", "low")
    # The thinking level changes the answers, so it is part of the judge's name.
    assert flash.name == "gemini-3.8-flash/thinking-low"
    assert all("latest" not in judge.model for judge in JUDGES.values())
    with pytest.raises(LlmConfigError, match="not a known judge"):
        resolve_judge("gpt-something")


def test_a_judge_with_no_thinking_level_is_named_by_its_model_alone():
    assert Judge("claude-opus-5-5", None, 1, provider="anthropic").name == "claude-opus-5-5"


def test_each_provider_reads_its_own_key_from_the_environment_only():
    gemini = Judge("gemini-3.8-flash", "low", 1)
    anthropic = Judge("claude-opus-5-5", None, 1, provider="anthropic")
    openai = Judge("gpt-x", None, 1, provider="openai")

    assert read_api_key(gemini, {"GEMINI_API_KEY": " abc "}) == "abc"
    assert read_api_key(anthropic, {"ANTHROPIC_API_KEY": "def"}) == "def"
    assert read_api_key(openai, {"OPENAI_API_KEY": "ghi"}) == "ghi"
    with pytest.raises(LlmConfigError, match="ANTHROPIC_API_KEY is not set"):
        read_api_key(anthropic, {"GEMINI_API_KEY": "abc"})


def test_a_provider_whose_sdk_is_not_installed_says_how_to_install_it(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def without_anthropic(name, *args, **kwargs):
        if name.startswith("system_one_adapter.providers.anthropic"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_anthropic)
    judge = Judge("claude-opus-5-5", None, 1, provider="anthropic")

    with pytest.raises(LlmConfigError, match=r"uv add 'system-one-adapter\[anthropic\]'"):
        make_provider(judge, {"ANTHROPIC_API_KEY": "abc"})
