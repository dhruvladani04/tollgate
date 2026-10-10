"""An experiment that is not part of the shipped policy.

The shipped band rules use `on_task` only to block a high-risk call. A change
that is easy to undo is therefore treated the same whether the user asked for
it or not. This experiment adds one rule, to see what it would buy:

    a call the engine would allow is held instead when it is off task and its
    risk score is at or above a second, lower level.

It is applied on top of the real engine's bands, so everything else is unchanged.
"""

from evals.metrics import GateOutcome
from evals.tune import RISK_SCORES, Sample, bands, outcome
from tollgate.policy.schema import Thresholds

OFF_TASK_LEVELS = (0.3, 0.5, 0.7, 0.9)
CONFIDENCES = (0.0, 0.2, 0.4)
# With the extra rule doing the work for off-task calls, the general ask level can sit higher.
RISK_ASK_RANGE = (1.0, 2.2)


def off_task_rule_bands(
    samples: list[Sample], thresholds: Thresholds, off_task_ask: float
) -> list[str]:
    result = []
    for sample, band in zip(samples, bands(samples, thresholds), strict=True):
        answers = sample.answers or {}
        on_task = answers.get("on_task")
        if (
            band == "allow"
            and on_task is not None
            and on_task.probability < thresholds.on_task
            and answers["risk"].score >= off_task_ask
        ):
            band = "ask"
        result.append(band)
    return result


def tune_off_task_rule(
    train: list[Sample], fitted: Thresholds, floor: float
) -> tuple[Thresholds, float, GateOutcome]:
    """Fit the experiment on the training samples with the same objective as the real tuning.

    Among settings whose recall on risky calls is at least `floor`, pick the
    fewest unnecessary holds, then the most malicious calls blocked.
    """
    base = fitted.model_dump()
    best = None
    for risk_ask in RISK_SCORES:
        if not RISK_ASK_RANGE[0] <= risk_ask <= RISK_ASK_RANGE[1]:
            continue
        for destructive in (base["destructive"], 0.9, 0.99):
            for confidence in CONFIDENCES:
                candidate = Thresholds.model_validate(
                    base
                    | {
                        "risk_ask": risk_ask,
                        "risk_block": max(base["risk_block"], risk_ask),
                        "destructive": destructive,
                        "confidence": confidence,
                    }
                )
                for off_task_ask in OFF_TASK_LEVELS:
                    result = outcome(train, off_task_rule_bands(train, candidate, off_task_ask))
                    if result.recall_on_risky < floor:
                        continue
                    rank = (result.false_ask_rate, -result.block_recall, -risk_ask, confidence)
                    if best is None or rank < best[0]:
                        best = (rank, candidate, off_task_ask, result)
    if best is None:
        raise ValueError("no setting of the experiment reaches the recall floor")
    return best[1], best[2], best[3]
