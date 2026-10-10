"""The band engine: code turns a model's answers into allow, ask, block or escalate.

The model only supplies probabilities. Every comparison is against a threshold
from the policy file.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from tollgate.decide.base import Answer, NoulAnswer, ScoreAnswer
from tollgate.policy.schema import Thresholds


class Band(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    BLOCK = "block"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class Verdict:
    band: Band
    reasons: tuple[str, ...]
    source: Literal["prefilter", "model", "no_decision"]
    """Whether an exact check, the model's answers or the lack of an answer decided."""


def confidence(answer: Answer) -> float:
    """How far an answer is from a coin flip, from 0 to 1.

    A Score reports its own confidence. A Noul has none, so the distance of its
    probability from 0.5 is used, as the vendor's docs suggest.
    """
    if isinstance(answer, NoulAnswer):
        return abs(2 * answer.probability - 1)
    return answer.confidence


def no_decision(why: str) -> Verdict:
    """Fail closed: with no usable answer, a person decides. The call is never allowed."""
    return Verdict(Band.ASK, (f"no decision from the model ({why})",), "no_decision")


def decide_band(answers: Mapping[str, Answer], thresholds: Thresholds) -> Verdict:
    """Pick the band for a call from the model's answers.

    `on_task` and `exfiltration` are absent when the client sent no user request.
    The block rules then cannot fire, and a high-risk call is held. Nothing shows
    that such a call serves the user, so the lower ask level applies to it.
    """
    t = thresholds
    risk = answers.get("risk")
    destructive = answers.get("destructive")
    if not isinstance(risk, ScoreAnswer) or not isinstance(destructive, NoulAnswer):
        return Verdict(Band.ASK, ("the model did not answer the core questions",), "model")
    on_task = answers.get("on_task")
    exfiltration = answers.get("exfiltration")

    def unsure(answer: Answer) -> bool:
        return confidence(answer) < t.confidence

    high_risk = risk.score > t.risk_block
    # Between the two ask levels a call runs only if it is known to be on task.
    between_ask_levels = t.risk_ask_off_task <= risk.score < t.risk_ask
    # `on_task` decides something only for a high-risk call and between the ask
    # levels, so its uncertainty is ignored otherwise.
    decisive: dict[str, Answer] = {"risk": risk, "destructive": destructive}
    if isinstance(exfiltration, NoulAnswer):
        decisive["exfiltration"] = exfiltration
    if isinstance(on_task, NoulAnswer) and (high_risk or between_ask_levels):
        decisive["on_task"] = on_task
    uncertain = [name for name, answer in decisive.items() if unsure(answer)]

    exfiltrating = (
        isinstance(exfiltration, NoulAnswer)
        and not unsure(exfiltration)
        and exfiltration.probability > t.exfiltration
    )
    task_known = isinstance(on_task, NoulAnswer) and not unsure(on_task)
    on_the_task = task_known and on_task.probability >= t.on_task
    off_the_task = task_known and on_task.probability < t.on_task
    # A call cannot both serve the request and send data where the request never said.
    contradiction = exfiltrating and on_the_task

    if not contradiction:
        block = []
        if exfiltrating:
            block.append(
                "exfiltration: the call sends data to a destination the request does not name "
                f"({exfiltration.probability:.2f} is above {t.exfiltration:.2f})"
            )
        if high_risk and not unsure(risk) and off_the_task:
            block.append(
                f"high risk and off task (risk {risk.score:.2f} is above {t.risk_block:.2f}, "
                f"on_task {on_task.probability:.2f} is below {t.on_task:.2f})"
            )
        if block:
            return Verdict(Band.BLOCK, tuple(block), "model")

    if contradiction or uncertain:
        reasons = [
            f"{name} is uncertain (confidence {confidence(decisive[name]):.2f} "
            f"is below {t.confidence:.2f})"
            for name in uncertain
        ]
        if contradiction:
            reasons.append("on_task and exfiltration contradict each other")
        return Verdict(Band.ESCALATE, tuple(reasons), "model")

    ask = []
    if risk.score >= t.risk_ask:
        ask.append(f"risk {risk.score:.2f} is at or above {t.risk_ask:.2f}")
    elif between_ask_levels and off_the_task:
        ask.append(
            f"off task with risk {risk.score:.2f} at or above {t.risk_ask_off_task:.2f} "
            f"(on_task {on_task.probability:.2f} is below {t.on_task:.2f})"
        )
    elif between_ask_levels and not isinstance(on_task, NoulAnswer):
        ask.append(
            f"the user's request is unknown and risk {risk.score:.2f} is at or above "
            f"{t.risk_ask_off_task:.2f}"
        )
    if destructive.probability > t.destructive:
        ask.append(
            f"destructive: the call deletes or overwrites data "
            f"({destructive.probability:.2f} is above {t.destructive:.2f})"
        )
    if ask:
        return Verdict(Band.ASK, tuple(ask), "model")
    return Verdict(
        Band.ALLOW,
        (f"risk {risk.score:.2f} is below {t.risk_ask:.2f} and no answer is over its threshold",),
        "model",
    )
