"""The band engine: pure rules that turn a model's answers into allow, ask, block or escalate."""

import pytest

from tollgate.decide.base import NoulAnswer, ScoreAnswer
from tollgate.policy.engine import Band, confidence, decide_band
from tollgate.policy.schema import Thresholds

# The example policy's starting values.
T = Thresholds(
    risk_ask=1.0,
    risk_ask_off_task=0.5,
    risk_block=2.2,
    destructive=0.5,
    exfiltration=0.5,
    on_task=0.5,
    confidence=0.5,
)


def answers(
    risk: float = 0.0,
    risk_confidence: float = 1.0,
    destructive: float = 0.02,
    on_task: float | None = 0.97,
    exfiltration: float | None = 0.03,
) -> dict:
    """A full set of answers for a harmless, on-task call, with any of them replaced."""
    given = {
        "risk": ScoreAnswer(score=risk, probabilities=(1.0, 0, 0, 0), confidence=risk_confidence),
        "destructive": NoulAnswer(probability=destructive),
    }
    if on_task is not None:
        given["on_task"] = NoulAnswer(probability=on_task)
    if exfiltration is not None:
        given["exfiltration"] = NoulAnswer(probability=exfiltration)
    return given


def without_context(**changes) -> dict:
    """The answers a client gets when it sends no user request."""
    return answers(on_task=None, exfiltration=None, **changes)


def band(given: dict, thresholds: Thresholds = T) -> Band:
    return decide_band(given, thresholds).band


def test_noul_confidence_is_its_distance_from_a_coin_flip():
    assert confidence(NoulAnswer(probability=0.5)) == 0.0
    assert confidence(NoulAnswer(probability=1.0)) == 1.0
    assert confidence(NoulAnswer(probability=0.0)) == 1.0
    assert confidence(NoulAnswer(probability=0.75)) == pytest.approx(0.5)


def test_score_confidence_is_the_one_the_model_reported():
    answer = ScoreAnswer(score=1.4, probabilities=(0, 0.6, 0.4, 0), confidence=0.35)

    assert confidence(answer) == 0.35


@pytest.mark.parametrize("given", [answers(), without_context(), answers(risk=0.99)])
def test_a_low_risk_call_with_clear_answers_is_allowed(given):
    assert band(given) is Band.ALLOW


@pytest.mark.parametrize(
    "given",
    [
        answers(risk=1.0),
        answers(risk=1.6),
        answers(risk=2.2),
        answers(destructive=0.91),
        without_context(risk=1.5),
        without_context(destructive=0.91),
    ],
    ids=[
        "risk-at-ask",
        "risk-mid",
        "risk-at-block-edge",
        "destructive",
        "no-ctx-risk",
        "no-ctx-destr",
    ],
)
def test_a_risky_or_destructive_call_is_held_for_a_person(given):
    assert band(given) is Band.ASK


@pytest.mark.parametrize(
    "given",
    [answers(risk=2.6, on_task=0.95), without_context(risk=2.6)],
    ids=["on-task", "task-unknown"],
)
def test_a_high_risk_call_that_is_not_known_to_be_off_task_is_held_not_allowed(given):
    assert band(given) is Band.ASK


@pytest.mark.parametrize(
    "given",
    [
        answers(exfiltration=0.9, on_task=0.05),
        answers(risk=2.6, on_task=0.05),
        answers(risk=0.0, exfiltration=0.95, on_task=0.02),
    ],
    ids=["exfiltration", "high-risk-off-task", "low-risk-exfiltration"],
)
def test_exfiltration_or_a_high_risk_off_task_call_is_blocked(given):
    assert band(given) is Band.BLOCK


def test_an_off_task_call_below_the_lower_ask_level_is_still_allowed():
    """Reading something the user did not mention is not held."""
    assert band(answers(risk=0.0, on_task=0.03)) is Band.ALLOW
    assert band(answers(risk=0.49, on_task=0.03)) is Band.ALLOW
    assert band(without_context(risk=0.49)) is Band.ALLOW


@pytest.mark.parametrize(
    "given",
    [answers(risk=0.5, on_task=0.03), answers(risk=0.99, on_task=0.03)],
    ids=["at-the-lower-level", "just-under-risk-ask"],
)
def test_an_off_task_call_between_the_two_ask_levels_is_held(given):
    """A change nobody asked for is held at a lower risk than one the user asked for."""
    verdict = decide_band(given, T)

    assert verdict.band is Band.ASK
    assert any("off task" in reason for reason in verdict.reasons)


def test_an_on_task_call_between_the_two_ask_levels_is_allowed():
    assert band(answers(risk=0.7, on_task=0.97)) is Band.ALLOW


def test_without_a_user_request_the_lower_ask_level_applies():
    """Nothing shows the call serves the user, so it is treated like an off-task one."""
    verdict = decide_band(without_context(risk=0.7), T)

    assert verdict.band is Band.ASK
    assert any("request is unknown" in reason for reason in verdict.reasons)


def test_the_off_task_rule_is_off_when_the_two_ask_levels_are_equal():
    switched_off = T.model_copy(update={"risk_ask_off_task": T.risk_ask})

    assert band(answers(risk=0.7, on_task=0.03), switched_off) is Band.ALLOW
    assert band(without_context(risk=0.7), switched_off) is Band.ALLOW
    assert band(answers(risk=0.7, on_task=0.6), switched_off) is Band.ALLOW
    assert band(answers(risk=1.0, on_task=0.97), switched_off) is Band.ASK


@pytest.mark.parametrize(
    "given",
    [
        answers(destructive=0.47),
        answers(risk=0.4, risk_confidence=0.3),
        answers(risk=2.6, on_task=0.6),
        answers(risk=0.7, on_task=0.6),
        answers(exfiltration=0.4),
        answers(exfiltration=0.6, on_task=0.05),
        without_context(destructive=0.7),
    ],
    ids=[
        "destructive",
        "risk-spread",
        "on-task-high-risk",
        "on-task-between-ask-levels",
        "exfil-below",
        "exfil-above",
        "no-ctx",
    ],
)
def test_an_uncertain_answer_escalates_whatever_side_of_its_threshold_it_is_on(given):
    assert band(given) is Band.ESCALATE


def test_uncertainty_about_the_task_is_ignored_when_it_cannot_change_the_band():
    """`on_task` decides nothing below the lower ask level, or from there up to `risk_block`
    once `risk_ask` is reached."""
    assert band(answers(risk=0.0, on_task=0.6)) is Band.ALLOW
    assert band(answers(risk=0.49, on_task=0.6)) is Band.ALLOW
    assert band(answers(risk=1.5, on_task=0.5)) is Band.ASK


def test_answers_that_contradict_each_other_escalate_instead_of_blocking():
    """The call cannot both serve the request and send data where the request never said."""
    assert band(answers(on_task=0.95, exfiltration=0.9)) is Band.ESCALATE


def test_a_clear_block_signal_is_not_diluted_by_an_unrelated_uncertain_answer():
    assert band(answers(exfiltration=0.95, on_task=0.03, destructive=0.5)) is Band.BLOCK


def test_no_call_is_allowed_while_any_answer_is_uncertain():
    steps = [step / 20 for step in range(21)]
    for name in ("destructive", "exfiltration"):
        for probability in steps:
            given = answers(**{name: probability})
            if confidence(given[name]) < T.confidence:
                assert band(given) is not Band.ALLOW, (name, probability)


def test_missing_core_answers_are_held_not_allowed():
    assert band({"destructive": NoulAnswer(probability=0.01)}) is Band.ASK
    assert band({}) is Band.ASK


def test_thresholds_come_from_the_policy_not_from_the_engine():
    cautious = T.model_copy(update={"destructive": 0.1, "risk_ask": 0.5, "risk_ask_off_task": 0.2})

    assert band(answers(destructive=0.2)) is Band.ALLOW
    assert band(answers(destructive=0.2), cautious) is Band.ASK
    assert band(answers(risk=0.7)) is Band.ALLOW
    assert band(answers(risk=0.7), cautious) is Band.ASK
    assert band(answers(risk=0.3, on_task=0.03)) is Band.ALLOW
    assert band(answers(risk=0.3, on_task=0.03), cautious) is Band.ASK


def test_reasons_name_the_signal_that_decided_the_band():
    blocked = decide_band(answers(exfiltration=0.9, on_task=0.05), T)
    held = decide_band(answers(destructive=0.91), T)
    escalated = decide_band(answers(destructive=0.47), T)
    allowed = decide_band(answers(), T)

    assert any("exfiltration" in reason for reason in blocked.reasons)
    assert any("destructive" in reason for reason in held.reasons)
    assert any("destructive" in reason and "uncertain" in reason for reason in escalated.reasons)
    assert allowed.reasons
    assert {blocked.source, held.source, escalated.source, allowed.source} == {"model"}
