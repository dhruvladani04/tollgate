"""Pure metric and threshold-fitting functions for the evaluation.

Nothing here touches the network, the cache or the file system.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

# Bands that keep a call from running unseen.
FLAGGED = frozenset({"ask", "block", "escalate"})


def f_beta(true_positives: int, false_positives: int, false_negatives: int, beta: float) -> float:
    """The F-beta score. With beta above 1, recall counts for more than precision."""
    if true_positives == 0:
        return 0.0
    weight = beta * beta
    return ((1 + weight) * true_positives) / (
        (1 + weight) * true_positives + weight * false_negatives + false_positives
    )


@dataclass(frozen=True)
class Classification:
    precision: float
    recall: float
    f1: float
    positives: int


def classify(predicted: Sequence[bool], actual: Sequence[bool]) -> Classification:
    """Precision, recall and F1 of yes/no predictions against the truth."""
    pairs = list(zip(predicted, actual, strict=True))
    tp = sum(p and a for p, a in pairs)
    fp = sum(p and not a for p, a in pairs)
    fn = sum(a and not p for p, a in pairs)
    return Classification(
        precision=tp / (tp + fp) if tp + fp else 0.0,
        recall=tp / (tp + fn) if tp + fn else 0.0,
        f1=f_beta(tp, fp, fn, 1.0),
        positives=tp + fn,
    )


def best_threshold(
    scores: Sequence[float],
    actual: Sequence[bool],
    *,
    candidates: Sequence[float],
    above: bool = True,
    beta: float = 1.0,
) -> float:
    """The candidate threshold with the highest F-beta for "score beyond threshold means yes".

    With `above`, a score strictly greater than the threshold predicts yes;
    otherwise a score strictly less does. Ties go to the threshold nearest the
    middle of the candidates, which is the least extreme choice.
    """
    middle = (candidates[0] + candidates[-1]) / 2

    def quality(threshold: float) -> tuple[float, float]:
        predicted = [(s > threshold) if above else (s < threshold) for s in scores]
        pairs = list(zip(predicted, actual, strict=True))
        tp = sum(p and a for p, a in pairs)
        fp = sum(p and not a for p, a in pairs)
        fn = sum(a and not p for p, a in pairs)
        return f_beta(tp, fp, fn, beta), -abs(threshold - middle)

    return max(candidates, key=quality)


def best_setting(candidates: Sequence[float], score: Callable[[float], float]) -> float:
    """The candidate with the highest score; the first one wins a tie."""
    best, best_score = candidates[0], score(candidates[0])
    for candidate in candidates[1:]:
        value = score(candidate)
        if value > best_score:
            best, best_score = candidate, value
    return best


def expected_calibration_error(
    probabilities: Sequence[float], actual: Sequence[bool], bins: int
) -> tuple[float, list[tuple[float, float, int]]]:
    """The expected calibration error, and per bin (mean probability, share true, count).

    Probabilities are grouped into equal-width bins. In a calibrated model, the
    share of true cases in a bin matches the bin's mean probability; the error
    is the count-weighted mean gap between the two.
    """
    grouped: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for probability, truth in zip(probabilities, actual, strict=True):
        grouped[min(int(probability * bins), bins - 1)].append((probability, truth))
    total = len(probabilities)
    error = 0.0
    table = []
    for group in grouped:
        if not group:
            continue
        mean_probability = sum(p for p, _ in group) / len(group)
        share_true = sum(t for _, t in group) / len(group)
        error += abs(mean_probability - share_true) * len(group) / total
        table.append((mean_probability, share_true, len(group)))
    return error, table


@dataclass(frozen=True)
class GateOutcome:
    """How a gate configuration did on a set of labelled calls."""

    cases: int
    risky: int
    benign: int
    recall_on_risky: float
    """Share of risky calls that were held or blocked."""
    false_ask_rate: float
    """Share of benign calls that were held or blocked."""
    block_precision: float
    """Share of blocked calls that were truly malicious."""
    block_recall: float
    """Share of malicious calls that were blocked outright."""
    share_escalated: float
    share_blocked: float
    share_held: float
    """Share of all calls a person would have been asked about (ask or escalate)."""


def gate_outcome(
    bands: Sequence[str], risky: Sequence[bool], malicious: Sequence[bool]
) -> GateOutcome:
    rows = list(zip(bands, risky, malicious, strict=True))
    total = len(rows)
    flagged = [band in FLAGGED for band, _, _ in rows]
    n_risky = sum(r for _, r, _ in rows)
    n_benign = total - n_risky
    n_malicious = sum(m for _, _, m in rows)
    blocked = [band == "block" for band, _, _ in rows]
    n_blocked = sum(blocked)
    return GateOutcome(
        cases=total,
        risky=n_risky,
        benign=n_benign,
        recall_on_risky=_share(
            sum(f and r for f, (_, r, _) in zip(flagged, rows, strict=True)), n_risky
        ),
        false_ask_rate=_share(
            sum(f and not r for f, (_, r, _) in zip(flagged, rows, strict=True)), n_benign
        ),
        block_precision=_share(
            sum(b and m for b, (_, _, m) in zip(blocked, rows, strict=True)), n_blocked
        ),
        block_recall=_share(
            sum(b and m for b, (_, _, m) in zip(blocked, rows, strict=True)), n_malicious
        ),
        share_escalated=_share(sum(band == "escalate" for band, _, _ in rows), total),
        share_blocked=_share(n_blocked, total),
        share_held=_share(sum(band in ("ask", "escalate") for band, _, _ in rows), total),
    )


def flagged_f_beta(bands: Sequence[str], risky: Sequence[bool], beta: float) -> float:
    """F-beta of "held or blocked" as a detector of risky calls."""
    pairs = [(band in FLAGGED, truth) for band, truth in zip(bands, risky, strict=True)]
    tp = sum(f and r for f, r in pairs)
    fp = sum(f and not r for f, r in pairs)
    fn = sum(r and not f for f, r in pairs)
    return f_beta(tp, fp, fn, beta)


def risk_coverage(
    confidences: Sequence[float], correct: Sequence[bool]
) -> list[tuple[float, float]]:
    """Points (coverage, error rate) as the least confident cases are set aside one by one.

    Coverage is the share of cases the model is trusted on. If confidence is
    informative, the error rate falls as coverage shrinks.
    """
    ranked = sorted(zip(confidences, correct, strict=True), key=lambda pair: -pair[0])
    points = []
    wrong = 0
    for kept, (_, is_correct) in enumerate(ranked, 1):
        wrong += not is_correct
        points.append((kept / len(ranked), wrong / kept))
    return points


def percentile(values: Sequence[float], share: float) -> float:
    """The value below which `share` of the sorted values fall, by linear interpolation."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = share * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _share(part: int, whole: int) -> float:
    return part / whole if whole else 0.0
