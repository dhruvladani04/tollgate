"""Fit the policy's thresholds on the training split.

Run with `python -m evals.tune` to print the fitted values. Nothing here calls
the model: it reads the cached answers.

The fit has two stages.

1. Each yes/no question gets the threshold that maximises F1 against its own
   label on the training cases: `destructive`, `exfiltration`, and `on_task`
   (as a detector of off-task calls). `risk_block` is the score threshold that
   best separates calls that are hard to undo or worse from the rest.
2. Two numbers have no label of their own, because they only mean something in
   combination: `risk_ask` and `confidence`. They are chosen by running the
   real band engine over the training cases. Among the settings that catch at
   least as many risky calls as the starting policy does, the one with the
   fewest unnecessary holds wins; ties go to the one that blocks more of the
   malicious calls outright.

So tuning is never allowed to buy fewer questions with weaker detection.
"""

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
    return fitted


def tune(train: list[Sample], start: Thresholds) -> Thresholds:
    """Fit every threshold on the training samples, starting from `start`."""
    fitted = fit_question_thresholds(train, start)
    floor = outcome(train, bands(train, start)).recall_on_risky

    best: tuple[tuple[float, float, float, float], dict[str, float]] | None = None
    for risk_ask in RISK_SCORES:
        if risk_ask > fitted["risk_block"]:
            break
        for confidence in CONFIDENCES:
            candidate = fitted | {"risk_ask": risk_ask, "confidence": confidence}
            result = outcome(train, bands(train, Thresholds.model_validate(candidate)))
            if result.recall_on_risky < floor:
                continue
            # Fewest unnecessary holds, then most malicious calls blocked; after that,
            # the least intrusive setting.
            rank = (result.false_ask_rate, -result.block_recall, -risk_ask, confidence)
            if best is None or rank < best[0]:
                best = (rank, candidate)
    return Thresholds.model_validate(best[1] if best is not None else fitted)


def fitted_policies() -> tuple[Policy, Policy, Policy]:
    """The start policy, the policy tuned with user requests, and the one tuned without."""
    start = start_policy()
    cache = DecisionCache(CACHE)
    train = [case for case in load_cases(GOLDEN) if case["split"] == "train"]
    with_request = tune(load_samples(train, cache, start, True), start.thresholds)
    without_request = tune(load_samples(train, cache, start, False), start.thresholds)
    return start, with_thresholds(start, with_request), with_thresholds(start, without_request)


def main() -> None:
    start, tuned, tuned_without = fitted_policies()
    print(f"{'threshold':<14}{'start':>8}{'tuned':>8}{'tuned, no request':>20}")
    for name in Thresholds.model_fields:
        print(
            f"{name:<14}{getattr(start.thresholds, name):>8.2f}"
            f"{getattr(tuned.thresholds, name):>8.2f}"
            f"{getattr(tuned_without.thresholds, name):>20.2f}"
        )


if __name__ == "__main__":
    main()
