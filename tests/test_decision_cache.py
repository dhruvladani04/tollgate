import asyncio

import pytest
from fakes import DECISION, FakeDecisionModel

from tollgate.decide.base import DecisionError, DecisionRequest
from tollgate.decide.cache import CachedDecisionModel, DecisionCache, request_key

QUESTION = {"type": "noul", "instructions": "`proposed_call` deletes data"}
REQUEST = DecisionRequest(
    state={"proposed_call": {"tool": "rm", "arguments": {"path": "a", "force": True}}},
    questions={"destructive": QUESTION},
)


def test_the_key_ignores_the_order_in_which_fields_were_written():
    reordered = DecisionRequest(
        state={"proposed_call": {"arguments": {"force": True, "path": "a"}, "tool": "rm"}},
        questions={"destructive": dict(reversed(QUESTION.items()))},
    )

    assert request_key("jev", "jev-1.13.0", reordered) == request_key("jev", "jev-1.13.0", REQUEST)


@pytest.mark.parametrize(
    "other",
    [
        ("jev", "jev-1.13.0", REQUEST.model_copy(update={"state": {"proposed_call": {}}})),
        ("jev", "jev-1.13.0", REQUEST.model_copy(update={"questions": {"other": QUESTION}})),
        ("jev", "typesafe/jev-1.13", REQUEST),
        ("llm", "jev-1.13.0", REQUEST),
    ],
    ids=["state", "questions", "model", "backend"],
)
def test_the_key_changes_with_anything_that_could_change_the_answer(other):
    assert request_key(*other) != request_key("jev", "jev-1.13.0", REQUEST)


def test_a_repeated_request_is_answered_from_the_cache(tmp_path):
    backend = FakeDecisionModel()
    model = CachedDecisionModel(backend, DecisionCache(tmp_path / "cache.jsonl"))

    first = asyncio.run(model.decide(REQUEST))
    second = asyncio.run(model.decide(REQUEST))

    assert len(backend.requests) == 1
    assert first == DECISION
    assert not first.cached
    assert second == DECISION.model_copy(update={"cached": True})


def test_the_cache_survives_a_restart(tmp_path):
    path = tmp_path / "state" / "cache.jsonl"
    asyncio.run(CachedDecisionModel(FakeDecisionModel(), DecisionCache(path)).decide(REQUEST))
    backend = FakeDecisionModel()

    decision = asyncio.run(CachedDecisionModel(backend, DecisionCache(path)).decide(REQUEST))

    assert backend.requests == []
    assert decision.cached
    assert decision.answers == DECISION.answers


def test_a_failed_decision_is_not_cached(tmp_path):
    cache = DecisionCache(tmp_path / "cache.jsonl")
    failing = CachedDecisionModel(FakeDecisionModel(DecisionError("timeout", "too slow")), cache)

    with pytest.raises(DecisionError):
        asyncio.run(failing.decide(REQUEST))

    assert len(cache) == 0


def test_an_unreadable_line_is_skipped_and_the_rest_still_load(tmp_path):
    path = tmp_path / "cache.jsonl"
    asyncio.run(CachedDecisionModel(FakeDecisionModel(), DecisionCache(path)).decide(REQUEST))
    with path.open("a", encoding="utf-8") as file:
        file.write('{"key": "cut-short", "decis\n')

    cache = DecisionCache(path)

    assert len(cache) == 1
    assert cache.get(request_key("fake", "fake-1", REQUEST)) == DECISION
