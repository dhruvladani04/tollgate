"""The two configurations that use an LLM judge, computed from the cached answers.

- Judge on every call: the judge's answers go through the same band engine and
  thresholds as Jev's.
- Jev plus escalation: Jev decides, and only a call it leaves uncertain is put
  to the judge. If the judge is uncertain too, or gave no answer, a person is
  still asked.
"""

import json
import statistics
from dataclasses import dataclass

from evals.data import Case, exact_verdict
from evals.judge import cache_path, failures_path, key_for, timings_path
from evals.metrics import percentile
from evals.tune import Sample, bands
from tollgate.decide.cache import DecisionCache
from tollgate.decide.llm import resolve_judge
from tollgate.policy.schema import Policy, Thresholds

# What a call with no usable answer gets: the gate fails closed and asks a person.
NO_ANSWER_BAND = "ask"


@dataclass(frozen=True)
class JudgeRun:
    """One judge's cached answers for a set of cases, in the cases' order."""

    samples: list[Sample]
    unanswered: int
    """Cases the judge was asked about and gave no usable answer for."""


def load_judge_run(cases: list[Case], judge_key: str, policy: Policy) -> JudgeRun:
    judge = resolve_judge(judge_key)
    cache = DecisionCache(cache_path(judge_key))
    samples, unanswered = [], 0
    for case in cases:
        verdict = exact_verdict(case, policy)
        if verdict is not None:
            samples.append(Sample(case, verdict.band.value, None))
            continue
        decision = cache.get(key_for(judge, case))
        if decision is None:
            unanswered += 1
            samples.append(Sample(case, NO_ANSWER_BAND, None))
        else:
            samples.append(Sample(case, None, decision.answers))
    return JudgeRun(samples, unanswered)


def escalated_bands(
    jev: list[Sample], judge: list[Sample], thresholds: Thresholds
) -> tuple[list[str], int]:
    """Jev's band for each call, with each uncertain one replaced by the judge's.

    Returns the bands and how many calls were sent to the judge.
    """
    result, sent = [], 0
    for own, second in zip(bands(jev, thresholds), bands(judge, thresholds), strict=True):
        if own == "escalate":
            sent += 1
            own = second
        result.append(own)
    return result, sent


def judge_usage(judge_key: str, cases: list[Case]) -> tuple[float, float, float, float]:
    """Mean input and output tokens per answered call, and median and p95 time in ms."""
    judge = resolve_judge(judge_key)
    cache = DecisionCache(cache_path(judge_key))
    decisions = [d for case in cases if (d := cache.get(key_for(judge, case))) is not None]
    wanted = {case["id"] for case in cases}
    timings = [
        entry["latency_ms"]
        for line in timings_path(judge_key).read_text(encoding="utf-8").splitlines()
        if (entry := json.loads(line))["case"] in wanted
    ]
    return (
        statistics.mean(d.input_tokens or 0 for d in decisions),
        statistics.mean(d.output_tokens or 0 for d in decisions),
        percentile(timings, 0.5),
        percentile(timings, 0.95),
    )


def recorded_failures(judge_key: str) -> int:
    path = failures_path(judge_key)
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0
