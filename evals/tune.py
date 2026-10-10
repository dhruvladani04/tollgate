"""Fit the policy's thresholds on the training split.

Run with `python -m evals.tune` to print the fitted values. Nothing here calls
the model: it reads the cached answers.

The fit has two stages.

1. Each yes/no question gets the threshold that maximises F1 against its own
   label on the training cases: `destructive`, `exfiltration`, and `on_task`
   (as a detector of off-task calls). `risk_block` is the score threshold that
   best separates calls that are hard to undo or worse from the rest.
2. Three numbers have no label of their own, because they only mean something
   in combination: `risk_ask`, `risk_ask_off_task` and `confidence`. They are
   chosen together by running the real band engine over the training cases.
   Among the settings that catch at least as many risky calls as the starting
   policy does, the one with the fewest unnecessary holds wins; ties go to the
   one that blocks more of the malicious calls outright.

So tuning is never allowed to buy fewer questions with weaker detection.

A narrower fit runs stage 2 alone, over the two ask levels, and leaves every
other threshold at its starting value. No threshold that takes part in a block
decision moves, so the gate blocks exactly what it blocked before.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from evals.data import (
    BACKEND,
    CACHE,
    MODEL,
    Case,
    exact_verdict,
    load_cases,
    request_for,
    start_policy,
    with_thresholds,
)
from evals.data import GOLDEN_CALLS as GOLDEN
from evals.metrics import GateOutcome, best_threshold, gate_outcome
from tollgate.decide.base import Answer
from tollgate.decide.cache import DecisionCache, request_key
from tollgate.policy.engine import decide_band
from tollgate.policy.schema import RISK_TOP, Policy, Thresholds

PROBABILITIES = [step / 100 for step in range(1, 100)]
RISK_SCORES = [step / 20 for step in range(int(RISK_TOP * 20) + 1)]
# The two ask levels are searched together, so they use a coarser grid.
ASK_LEVELS = RISK_SCORES[::2]
CONFIDENCES = [step / 20 for step in range(20)]
# The label that "high risk" is fitted against: hard to undo, or worse.
HIGH_RISK_LEVEL = 2


@dataclass(frozen=True)
class Sample:
    """One case with everything needed to compute its band under any thresholds."""

    case: Case
    exact_band: str | None
    """The band the pre-filter chose, or None when the call goes to the model."""
    answers: dict[str, Answer] | None
    """The model's cached answers, or None when the pre-filter decided."""


class MissingAnswers(Exception):
    """The cache has no answer for some case; `python -m evals.run` fills it."""


def load_samples(
    cases: list[Case], cache: DecisionCache, policy: Policy, with_user_request: bool
) -> list[Sample]:
    samples = []
    missing = 0
    for case in cases:
        verdict = exact_verdict(case, policy)
        if verdict is not None:
            samples.append(Sample(case, verdict.band.value, None))
            continue
        key = request_key(BACKEND, MODEL, request_for(case, with_user_request))
        decision = cache.get(key)
        if decision is None:
            missing += 1
            continue
        samples.append(Sample(case, None, decision.answers))
    if missing:
        raise MissingAnswers(
            f"{missing} cases have no cached answer; run `python -m evals.run` with an API key"
        )
    return samples


def bands(samples: list[Sample], thresholds: Thresholds) -> list[str]:
    """The band each sample lands in: the pre-filter's, else the engine's on the answers."""
    return [
        sample.exact_band or decide_band(sample.answers or {}, thresholds).band.value
        for sample in samples
    ]


def rules_only_bands(samples: list[Sample]) -> list[str]:
    """With no model, a call the pre-filter does not decide simply runs."""
    return [sample.exact_band or "allow" for sample in samples]


def outcome(samples: list[Sample], band_list: list[str]) -> GateOutcome:
    return gate_outcome(
        band_list,
        [sample.case["risky"] for sample in samples],
        [sample.case["malicious"] for sample in samples],
    )


def column(samples: list[Sample], question: str) -> tuple[list[float], list[bool]]:
    """The model's number for one question beside its true label, for cases that have both.

    For `risk` the number is the score and the label is "hard to undo or worse".
    """
    scores, labels = [], []
    for sample in samples:
        answer = (sample.answers or {}).get(question)
        if answer is None:
            continue
        if question == "risk":
            scores.append(answer.score)
            labels.append(sample.case["truth"]["risk"] >= HIGH_RISK_LEVEL)
        else:
            scores.append(answer.probability)
            labels.append(bool(sample.case["truth"][question]))
    return scores, labels


def fit_question_thresholds(train: list[Sample], start: Thresholds) -> dict[str, float]:
    """Stage 1: fit each question's threshold to its own label."""
    fitted = start.model_dump()
    scores, labels = column(train, "destructive")
    fitted["destructive"] = best_threshold(scores, labels, candidates=PROBABILITIES)
    scores, labels = column(train, "exfiltration")
    if any(labels):
        fitted["exfiltration"] = best_threshold(scores, labels, candidates=PROBABILITIES)
    scores, labels = column(train, "on_task")
    if scores:
        off_task = [not label for label in labels]
        fitted["on_task"] = best_threshold(scores, off_task, candidates=PROBABILITIES, above=False)
    scores, labels = column(train, "risk")
    fitted["risk_block"] = best_threshold(scores, labels, candidates=RISK_SCORES)
    fitted["risk_ask"] = min(fitted["risk_ask"], fitted["risk_block"])
    fitted["risk_ask_off_task"] = min(fitted["risk_ask_off_task"], fitted["risk_ask"])
    return fitted


def ask_settings(
    fitted: dict[str, float],
    off_task_rule: bool = True,
    confidences: Sequence[float] = CONFIDENCES,
) -> Iterator[Thresholds]:
    """Stage 2's candidates: every combination of the numbers that have no label.

    Without `off_task_rule` the two ask levels are kept equal, which switches
    the off-task rule off.
    """
    for risk_ask in ASK_LEVELS:
        if risk_ask > fitted["risk_block"]:
            break
        lower_levels = (
            [level for level in ASK_LEVELS if level <= risk_ask] if off_task_rule else [risk_ask]
        )
        for off_task in lower_levels:
            for confidence in confidences:
                yield Thresholds.model_validate(
                    fitted
                    | {
                        "risk_ask": risk_ask,
                        "risk_ask_off_task": off_task,
                        "confidence": confidence,
                    }
                )


def tune(
    train: list[Sample],
    start: Thresholds,
    off_task_rule: bool = True,
    ask_levels_only: bool = False,
) -> Thresholds:
    """Fit the thresholds on the training samples, starting from `start`.

    With `ask_levels_only`, only `risk_ask` and `risk_ask_off_task` are fitted and
    every other threshold keeps its starting value.
    """
    if ask_levels_only:
        fitted, confidences = start.model_dump(), [start.confidence]
    else:
        fitted, confidences = fit_question_thresholds(train, start), CONFIDENCES
    floor = outcome(train, bands(train, start)).recall_on_risky

    best: tuple[tuple[float, ...], Thresholds] | None = None
    for candidate in ask_settings(fitted, off_task_rule, confidences):
        result = outcome(train, bands(train, candidate))
        if result.recall_on_risky < floor:
            continue
        # Fewest unnecessary holds, then most malicious calls blocked; after that,
        # the least intrusive setting.
        rank = (
            result.false_ask_rate,
            -result.block_recall,
            -candidate.risk_ask,
            -candidate.risk_ask_off_task,
            candidate.confidence,
        )
        if best is None or rank < best[0]:
            best = (rank, candidate)
    return best[1] if best is not None else Thresholds.model_validate(fitted)


def fitted_policies() -> tuple[Policy, Policy, Policy]:
    """The start policy, the one with only its ask levels tuned, and the fully tuned one."""
    start = start_policy()
    train = [case for case in load_cases(GOLDEN) if case["split"] == "train"]
    samples = load_samples(train, DecisionCache(CACHE), start, with_user_request=True)
    return (
        start,
        with_thresholds(start, tune(samples, start.thresholds, ask_levels_only=True)),
        with_thresholds(start, tune(samples, start.thresholds)),
    )


def main() -> None:
    start, ask_tuned, tuned = fitted_policies()
    print(f"{'threshold':<20}{'start':>8}{'ask levels tuned':>18}{'tuned':>8}")
    for name in Thresholds.model_fields:
        print(
            f"{name:<20}{getattr(start.thresholds, name):>8.2f}"
            f"{getattr(ask_tuned.thresholds, name):>18.2f}"
            f"{getattr(tuned.thresholds, name):>8.2f}"
        )


if __name__ == "__main__":
    main()
