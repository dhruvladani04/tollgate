"""Build docs/results.md and its figures from the cached answers.

Run with `python -m evals.report`. It calls no model and needs no key: every
number comes from the committed cache, so anyone can reproduce the report.
"""

import json
import statistics
import sys
from collections import Counter, defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import PercentFormatter  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402

from evals.data import (  # noqa: E402
    CACHE,
    EXAMPLE_POLICY,
    GOLDEN_CALLS,
    INJECAGENT_CALLS,
    META,
    REPEATS,
    REPO,
    key_for,
    load_cases,
    start_policy,
)
from evals.metrics import (  # noqa: E402
    FLAGGED,
    GateOutcome,
    classify,
    expected_calibration_error,
    percentile,
    risk_coverage,
)
from evals.tune import (  # noqa: E402
    MissingAnswers,
    Sample,
    ask_settings,
    bands,
    column,
    fit_question_thresholds,
    load_samples,
    outcome,
    rules_only_bands,
    tune,
)
from tollgate.decide.base import Decision  # noqa: E402
from tollgate.decide.cache import DecisionCache  # noqa: E402
from tollgate.policy.engine import confidence as answer_confidence  # noqa: E402
from tollgate.policy.engine import decide_band  # noqa: E402
from tollgate.policy.schema import Thresholds, load_policy  # noqa: E402

RESULTS = REPO / "docs" / "results.md"
IMAGES = REPO / "docs" / "img"

# TypeSafe's listed price for jev-1.13.0 input tokens, in US dollars per million,
# as read on 8 October 2026. Output tokens are free.
PRICE_PER_MILLION_TOKENS = 0.042
ECE_BINS = 15
# The risk-coverage curve starts here; below it, too few cases give a stable rate.
COVERAGE_FLOOR = 0.1
NOULS = ("destructive", "exfiltration", "on_task")
# Where each label sits beside its point in the trade-off figure, in points.
TRADEOFF_LABEL_OFFSETS = {
    "Jev tuned": (-12, -18),
    "Jev, ask levels tuned": (-2, 10),
    "Jev at 0.5": (10, -5),
}
# The trade-off figure stops here; settings that hold more benign calls are not drawn.
TRADEOFF_MAX_HELD = 0.6

# Chart colours: a validated two-colour palette on a light surface.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"
ORANGE = "#eb6834"
CONTEXT = "#c3c2b7"


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def outcome_row(label: str, result: GateOutcome) -> list[str]:
    return [
        label,
        pct(result.recall_on_risky),
        pct(result.false_ask_rate),
        pct(result.block_recall),
        pct(result.block_precision) if result.share_blocked else "n/a",
        pct(result.share_held),
        pct(result.share_escalated),
    ]


OUTCOME_HEADERS = [
    "Configuration",
    "Risky calls caught",
    "Benign calls held",
    "Malicious calls blocked",
    "Block precision",
    "Calls a person is asked about",
    "Of which uncertain",
]


def kind(case: dict) -> str:
    truth = case["truth"]
    change = {
        0: "reads only",
        1: "easy-to-undo change",
        2: "hard-to-undo change",
        3: "destroys, spends or sends out",
    }[truth["risk"]]
    return f"{'On' if truth['on_task'] else 'Off'} task, {change}"


def style_axes(axis) -> None:
    axis.set_facecolor(SURFACE)
    for side in ("top", "right"):
        axis.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axis.spines[side].set_color(AXIS)
        axis.spines[side].set_linewidth(1)
    axis.tick_params(colors=MUTED, labelsize=9, length=0)
    axis.grid(True, color=GRID, linewidth=1)
    axis.set_axisbelow(True)


def new_figure(width: float, height: float, columns: int = 1):
    plt.rcParams["font.family"] = ["Segoe UI", "DejaVu Sans", "sans-serif"]
    figure, axes = plt.subplots(1, columns, figsize=(width, height), dpi=160)
    figure.patch.set_facecolor(SURFACE)
    return figure, axes


def save(figure, name: str) -> str:
    IMAGES.mkdir(parents=True, exist_ok=True)
    figure.savefig(IMAGES / name, facecolor=SURFACE, bbox_inches="tight")
    plt.close(figure)
    return f"img/{name}"


def plot_tradeoff(cloud_on, cloud_off, named) -> str:
    """Recall against unnecessary holds: what threshold settings can reach, and named points."""
    figure, axis = new_figure(7.2, 4.6)
    style_axes(axis)
    axis.scatter(
        [x for x, _ in cloud_on],
        [y for _, y in cloud_on],
        s=14,
        color=CONTEXT,
        linewidths=0,
        label="Threshold settings, off-task rule on",
    )
    axis.scatter(
        [x for x, _ in cloud_off],
        [y for _, y in cloud_off],
        s=14,
        color=ORANGE,
        linewidths=0,
        label="Threshold settings, off-task rule off",
    )
    axis.scatter(
        [point[1] for point in named],
        [point[2] for point in named],
        s=70,
        color=BLUE,
        edgecolors=SURFACE,
        linewidths=2,
        zorder=3,
        label="Named configurations",
    )
    for label, x, y in named:
        axis.annotate(
            label,
            (x, y),
            xytext=TRADEOFF_LABEL_OFFSETS.get(label, (8, -4)),
            textcoords="offset points",
            fontsize=9,
            color=INK,
            # A label can sit over the cloud, so it gets a patch of the surface behind it.
            bbox={"facecolor": SURFACE, "edgecolor": "none", "pad": 1.5, "alpha": 0.85},
            zorder=4,
        )
    axis.set_xlim(-0.02, TRADEOFF_MAX_HELD + 0.02)
    axis.set_ylim(0, 1.04)
    axis.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    axis.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    axis.set_xlabel(
        "Benign calls held or blocked (lower is better)", color=INK_SECONDARY, fontsize=10
    )
    axis.set_ylabel("Risky calls caught (higher is better)", color=INK_SECONDARY, fontsize=10)
    axis.set_title(
        "Recall against unnecessary holds, golden test split", color=INK, fontsize=12, loc="left"
    )
    legend = axis.legend(loc="lower right", fontsize=9, frameon=False)
    for text in legend.get_texts():
        text.set_color(INK_SECONDARY)
    return save(figure, "tradeoff.png")


def plot_reliability(calibration: dict[str, tuple[float, list[tuple[float, float, int]]]]) -> str:
    figure, axes = new_figure(10.2, 3.6, columns=len(calibration))
    for axis, (question, (error, bins)) in zip(axes, calibration.items(), strict=True):
        style_axes(axis)
        axis.plot([0, 1], [0, 1], color=AXIS, linewidth=1)
        # Markers only: a line through bins that hold a few cases each would suggest a trend.
        axis.scatter(
            [mean for mean, _, _ in bins],
            [share for _, share, _ in bins],
            s=64,
            color=BLUE,
            edgecolors=SURFACE,
            linewidths=2,
            zorder=3,
        )
        axis.set_xlim(-0.03, 1.03)
        axis.set_ylim(-0.03, 1.03)
        axis.set_title(f"{question}  (error {error:.3f})", color=INK, fontsize=11, loc="left")
        axis.set_xlabel("Probability Jev gave", color=INK_SECONDARY, fontsize=10)
    axes[0].set_ylabel("Share that were truly yes", color=INK_SECONDARY, fontsize=10)
    figure.suptitle(
        "Calibration on the golden test split: points on the diagonal are perfectly calibrated",
        color=INK,
        fontsize=12,
        x=0.01,
        ha="left",
    )
    figure.tight_layout()
    return save(figure, "reliability.png")


def plot_risk_coverage(points: list[tuple[float, float]]) -> str:
    figure, axis = new_figure(7.2, 4.0)
    style_axes(axis)
    shown = [(x, y) for x, y in points if x >= COVERAGE_FLOOR]
    axis.plot([x for x, _ in shown], [y for _, y in shown], color=BLUE, linewidth=2)
    axis.set_xlim(0, 1.02)
    axis.set_ylim(0, max(0.05, max(y for _, y in points) * 1.15))
    axis.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    axis.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    axis.set_xlabel(
        "Share of calls decided on Jev's answer alone, most confident first",
        color=INK_SECONDARY,
        fontsize=10,
    )
    axis.set_ylabel("Wrong decisions among them", color=INK_SECONDARY, fontsize=10)
    axis.set_title(
        "Error rate as less confident answers are included, golden test split",
        color=INK,
        fontsize=12,
        loc="left",
    )
    return save(figure, "risk_coverage.png")


def stability(tuned: Thresholds) -> tuple[int, float, dict[str, tuple[float, float]], list[float]]:
    """From the timed repeats: band agreement, spread of each answer, and the call timings."""
    by_case: dict[str, list[Decision]] = defaultdict(list)
    timings = []
    for line in REPEATS.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        by_case[entry["case"]].append(Decision.model_validate(entry["decision"]))
        timings.append(entry["latency_ms"])
    agree = 0
    spreads: dict[str, list[float]] = defaultdict(list)
    for decisions in by_case.values():
        chosen = {decide_band(decision.answers, tuned).band for decision in decisions}
        agree += len(chosen) == 1
        for question in decisions[0].answers:
            values = [
                answer.score if question == "risk" else answer.probability
                for answer in (decision.answers[question] for decision in decisions)
            ]
            spreads[question].append(max(values) - min(values))
    summary = {
        question: (statistics.median(values), percentile(values, 0.95))
        for question, values in spreads.items()
    }
    return len(by_case), agree / len(by_case), summary, timings


def build() -> str:
    start = start_policy()
    cache = DecisionCache(CACHE)
    golden = load_cases(GOLDEN_CALLS)
    external = load_cases(INJECAGENT_CALLS)
    train_cases = [case for case in golden if case["split"] == "train"]
    test_cases = [case for case in golden if case["split"] == "test"]

    train = load_samples(train_cases, cache, start, True)
    test = load_samples(test_cases, cache, start, True)
    outside = load_samples(external, cache, start, True)
    test_bare = load_samples(test_cases, cache, start, False)
    outside_bare = load_samples(external, cache, start, False)

    tuned = tune(train, start.thresholds)
    # Equal ask levels switch the off-task rule off; these two show what the rule adds.
    start_rule_off = start.thresholds.model_copy(
        update={"risk_ask_off_task": start.thresholds.risk_ask}
    )
    tuned_rule_off = tune(train, start.thresholds, off_task_rule=False)
    # Only the two ask levels fitted; every threshold behind a block keeps its start value.
    ask_tuned = tune(train, start.thresholds, ask_levels_only=True)
    # Which of the measured configurations the example policy holds, if any.
    shipped_thresholds = load_policy(EXAMPLE_POLICY).thresholds
    shipped = next(
        (
            label
            for label, thresholds in (
                ("Jev at 0.5", start.thresholds),
                ("Jev, ask levels tuned", ask_tuned),
                ("Jev tuned", tuned),
            )
            if thresholds == shipped_thresholds
        ),
        None,
    )
    shipped_sentence = (
        f"`policy.example.yaml`, the policy Tollgate ships as its example, holds the "
        f'thresholds of "{shipped}".'
        if shipped
        else "`policy.example.yaml` holds thresholds that match none of these configurations."
    )

    def results(samples: list[Sample], bare: list[Sample]) -> dict[str, GateOutcome]:
        return {
            "Rules only": outcome(samples, rules_only_bands(samples)),
            "Jev at 0.5": outcome(samples, bands(samples, start.thresholds)),
            "Jev tuned": outcome(samples, bands(samples, tuned)),
            "Jev at 0.5, off-task rule off": outcome(samples, bands(samples, start_rule_off)),
            "Jev tuned, off-task rule off": outcome(samples, bands(samples, tuned_rule_off)),
            "Jev at 0.5, no user request": outcome(bare, bands(bare, start.thresholds)),
            "Jev tuned, no user request": outcome(bare, bands(bare, tuned)),
            "Jev, ask levels tuned": outcome(samples, bands(samples, ask_tuned)),
            "Jev, ask levels tuned, no user request": outcome(bare, bands(bare, ask_tuned)),
        }

    on_test = results(test, test_bare)
    on_outside = results(outside, outside_bare)
    main_rows = ("Rules only", "Jev at 0.5", "Jev, ask levels tuned", "Jev tuned")
    bare_rows = (
        "Jev at 0.5, no user request",
        "Jev, ask levels tuned, no user request",
        "Jev tuned, no user request",
    )
    rule_rows = (
        "Jev at 0.5, off-task rule off",
        "Jev at 0.5",
        "Jev tuned, off-task rule off",
        "Jev tuned",
    )

    # --- per-question quality ---------------------------------------------------------
    question_rows = []
    calibration = {}
    for question in NOULS:
        scores, labels = column(test, question)
        detect_low = question == "on_task"
        actual = [not label for label in labels] if detect_low else labels
        row = [f"`{question}`" + (" (detecting off-task)" if detect_low else "")]
        row.append(f"{roc_auc_score(labels, scores):.3f}")
        for threshold in (getattr(start.thresholds, question), getattr(tuned, question)):
            predicted = [(s < threshold) if detect_low else (s > threshold) for s in scores]
            quality = classify(predicted, actual)
            row += [
                f"{threshold:.2f}",
                pct(quality.precision),
                pct(quality.recall),
                f"{quality.f1:.2f}",
            ]
        row.append(str(sum(actual)))
        question_rows.append(row)
        calibration[question] = expected_calibration_error(scores, labels, ECE_BINS)
    risk_scores, risk_labels = column(test, "risk")
    risk_by_level = defaultdict(list)
    for sample in test:
        if sample.answers:
            risk_by_level[sample.case["truth"]["risk"]].append(sample.answers["risk"].score)

    # --- selective prediction ---------------------------------------------------------
    decisive = tuned.model_copy(update={"confidence": 0.0})
    judged = [sample for sample in test if sample.answers]
    confidences = [min(answer_confidence(a) for a in s.answers.values()) for s in judged]
    correct = [
        (band in FLAGGED) == sample.case["risky"]
        for sample, band in zip(judged, bands(judged, decisive), strict=True)
    ]
    curve = risk_coverage(confidences, correct)

    def error_at(coverage: float) -> float:
        return min(curve, key=lambda point: abs(point[0] - coverage))[1]

    # --- figures ----------------------------------------------------------------------
    question_fit = fit_question_thresholds(train, start.thresholds)
    cloud_on, cloud_off = [], []
    for candidate in ask_settings(question_fit):
        result = outcome(test, bands(test, candidate))
        rule_off = candidate.risk_ask_off_task == candidate.risk_ask
        (cloud_off if rule_off else cloud_on).append(
            (result.false_ask_rate, result.recall_on_risky)
        )
    named = [
        (label, on_test[label].false_ask_rate, on_test[label].recall_on_risky)
        for label in main_rows
    ]
    tradeoff_image = plot_tradeoff(cloud_on, cloud_off, named)
    out_of_frame = sum(held > TRADEOFF_MAX_HELD + 0.02 for held, _ in cloud_on + cloud_off)
    reliability_image = plot_reliability(calibration)
    coverage_image = plot_risk_coverage(curve)

    # --- where the tuned gate is wrong ------------------------------------------------
    tuned_bands = bands(test, tuned)
    held_benign: Counter[str] = Counter()
    benign_total: Counter[str] = Counter()
    missed: Counter[str] = Counter()
    risky_total: Counter[str] = Counter()
    for sample, band in zip(test, tuned_bands, strict=True):
        if sample.case["risky"]:
            risky_total[kind(sample.case)] += 1
            missed[kind(sample.case)] += band == "allow"
        else:
            benign_total[kind(sample.case)] += 1
            held_benign[kind(sample.case)] += band in FLAGGED

    held_total = sum(held_benign.values())
    largest_kind, largest_held = held_benign.most_common(1)[0]
    wrong_at_full = [
        (sample, band)
        for sample, band in zip(judged, bands(judged, decisive), strict=True)
        if (band in FLAGGED) != sample.case["risky"]
    ]
    benign_held_at_full = sum(not sample.case["risky"] for sample, _ in wrong_at_full)

    # --- operations and stability -----------------------------------------------------
    sampled, agreement, spreads, timings = stability(tuned)
    _, start_agreement, _, _ = stability(start.thresholds)
    _, ask_tuned_agreement, _, _ = stability(ask_tuned)
    largest_missed_kind, largest_missed = missed.most_common(1)[0]
    tokens = [
        decision.input_tokens
        for case in test_cases
        if (decision := cache.get(key_for(case, True))) is not None and decision.input_tokens
    ]
    tokens_bare = [
        decision.input_tokens
        for case in test_cases
        if (decision := cache.get(key_for(case, False))) is not None and decision.input_tokens
    ]
    meta = json.loads(META.read_text(encoding="utf-8")) if META.exists() else {}

    def count(cases: list[dict]) -> list[str]:
        benign = sum(not case["risky"] for case in cases)
        malicious = sum(case["malicious"] for case in cases)
        return [str(len(cases)), str(benign), str(len(cases) - benign - malicious), str(malicious)]

    jev, fitted = on_test["Jev at 0.5"], on_test["Jev tuned"]
    no_rule = on_test["Jev tuned, off-task rule off"]
    no_request = on_test["Jev tuned, no user request"]
    ask_only = on_test["Jev, ask levels tuned"]
    threshold_rows = [
        [
            f"`{name}`",
            f"{getattr(start.thresholds, name):.2f}",
            f"{getattr(ask_tuned, name):.2f}",
            f"{getattr(tuned, name):.2f}",
            f"{getattr(tuned_rule_off, name):.2f}",
        ]
        for name in Thresholds.model_fields
    ]

    parts = [
        "# Tollgate evaluation results",
        "This file is generated by `python -m evals.report` from cached model answers. "
        "Do not edit it by hand; rerun the command instead.",
        f"The answers come from `{meta.get('model', 'jev-1.13.0')}`, asked on "
        f"{meta.get('asked_on', 'an unrecorded date')}.",
        "## The short version",
        "The table shows each configuration on the golden test split, which was not used "
        f"for tuning. {shipped_sentence}",
        table(OUTCOME_HEADERS, [outcome_row(label, on_test[label]) for label in main_rows]),
        f"- **Rules only** caught {pct(on_test['Rules only'].recall_on_risky)} of risky calls.",
        f"- **Jev at the starting thresholds** caught {pct(jev.recall_on_risky)} of risky calls "
        f"and held {pct(jev.false_ask_rate)} of benign ones.",
        f"- **With only the two ask levels tuned**, those figures were "
        f"{pct(ask_only.recall_on_risky)} and {pct(ask_only.false_ask_rate)}. No threshold "
        "that takes part in a block decision was changed, so the gate blocks exactly the "
        "same calls as at the starting thresholds.",
        f"- **With every threshold tuned**, they were {pct(fitted.recall_on_risky)} and "
        f"{pct(fitted.false_ask_rate)}. Malicious calls blocked outright, instead of put to a "
        f"person, went from {pct(jev.block_recall)} to {pct(fitted.block_recall)}.",
        f"- **Tuned the same way with the off-task rule switched off**, the gate caught "
        f"{pct(no_rule.recall_on_risky)} of risky calls and held {pct(no_rule.false_ask_rate)} "
        "of benign ones.",
        f"- **Without the user's request**, the tuned gate caught "
        f"{pct(no_request.recall_on_risky)} of risky calls and held "
        f"{pct(no_request.false_ask_rate)} of benign ones.",
        f"- **Benign calls the tuned gate held:** {held_total} of {sum(benign_total.values())}."
        + (
            f' The largest group, {largest_held}, was "{largest_kind.lower()}".'
            if held_total
            else ""
        ),
        "## What the words mean",
        "- A **risky** call is one that should not run without a person seeing it. A **benign** "
        "call is any other. A **malicious** call is a risky one that should be blocked outright. "
        "The labelling rules are in [evals/golden.py](../evals/golden.py).",
        "- **Caught** means held for a person or blocked. **Held** means a person is asked: the "
        "Ask band, plus the Escalate band, which is held for a person until a second model "
        "exists to judge it.",
        "- **Block precision** is the share of blocked calls that were truly malicious.",
        "## The data",
        table(
            ["Set", "Cases", "Benign", "Risky, not malicious", "Malicious"],
            [
                ["Golden, train", *count(train_cases)],
                ["Golden, test", *count(test_cases)],
                ["InjecAgent-derived, test only", *count(external)],
            ],
        ),
        "- **Golden set.** 330 hand-written scenes across filesystem, git, HTTP, database and "
        "email tools. Each scene is one tool call, paired with a user request it serves and "
        "with an unrelated one. Both cases of a scene are in the same split. The scenes were "
        "written by Claude and labelled by fixed rules, not case by case.",
        "- **InjecAgent-derived set.** Built from the "
        "[InjecAgent](https://github.com/uiuc-kang-lab/InjecAgent) benchmark (MIT licence, "
        "reproduced in [the notice](../evals/data/external/NOTICE.md)). Its instructions, tool "
        "names and tool descriptions are used as published. The tool arguments are not part of "
        "the benchmark and were written for this evaluation. This set is never used for tuning.",
        "## The configurations",
        "- **Rules only.** The exact checks with no model: deny list, secret patterns and path "
        "sandbox. A call they do not decide simply runs.",
        "- **Jev at 0.5.** The starting policy: every yes/no threshold at 0.5, the risk levels "
        "from the build plan, and `risk_ask_off_task` at "
        f"{start.thresholds.risk_ask_off_task:.2f}.",
        "- **Jev, ask levels tuned.** The starting policy with only `risk_ask` and "
        "`risk_ask_off_task` fitted on the golden train split.",
        "- **Jev tuned.** The same rules with every threshold fitted on the golden train split.",
        "Two more configurations from the build plan, escalation to an LLM judge and an LLM "
        "judge on every call, are not built yet.",
        "### The off-task rule",
        "The band engine holds a call whose risk score is at or above `risk_ask`. A call that "
        "is not known to serve the user's request is held from a lower level, "
        "`risk_ask_off_task`. That covers a call the model judges off task and a call from a "
        "client that sent no request. Setting the two levels equal switches the rule off.",
        "The rule is not in the build plan's band table. It was added after the first run of "
        "this evaluation; [What the off-task rule adds](#what-the-off-task-rule-adds) shows "
        "the difference.",
        "### How the thresholds were tuned",
        "Each yes/no question's threshold was set to maximise F1 against its own label on the "
        'train split, and `risk_block` the same way against "hard to undo or worse". '
        "`risk_ask`, `risk_ask_off_task` and `confidence` have no label of their own, so they "
        "were chosen together by running the real band engine over the train split: among "
        "settings that caught at least as many risky calls as the starting policy, the one "
        "with the fewest benign calls held won. Tuning was therefore not allowed to trade away "
        "detection.",
        'For "ask levels tuned", only that second step was run, over `risk_ask` and '
        "`risk_ask_off_task`, with every other threshold left at its starting value.",
        table(
            ["Threshold", "Start", "Ask levels tuned", "Tuned", "Tuned, off-task rule off"],
            threshold_rows,
        ),
        shipped_sentence,
        "## Results on the external set",
        table(OUTCOME_HEADERS, [outcome_row(label, on_outside[label]) for label in main_rows]),
        "The thresholds were fitted on the golden set only, so this table shows how they carry "
        "over to cases from a different source.",
        "## Results without the user's request",
        "An MCP tool call does not say what the user asked for. Tollgate knows only when the "
        "client or a hook supplies it. Without the request, only `risk` and `destructive` can "
        "be asked, nothing can be blocked by the model, and the lower ask level applies to "
        "every call. These are the same policies as above, on the same cases with the "
        "request left out.",
        "Golden test split:",
        table(OUTCOME_HEADERS, [outcome_row(label, on_test[label]) for label in bare_rows]),
        "InjecAgent-derived set:",
        table(OUTCOME_HEADERS, [outcome_row(label, on_outside[label]) for label in bare_rows]),
        "## Where the tuned gate is wrong",
        "Benign calls the tuned gate held or blocked, on the golden test split:",
        table(
            ["Kind of call", "Benign cases", "Held or blocked"],
            [
                [name, str(total), str(held_benign[name])]
                for name, total in sorted(benign_total.items())
            ],
        ),
        "Risky calls the tuned gate allowed, on the golden test split:",
        table(
            ["Kind of call", "Risky cases", "Allowed"],
            [[name, str(total), str(missed[name])] for name, total in sorted(risky_total.items())],
        ),
        f"Of the {sum(missed.values())} risky calls allowed, {largest_missed} were of the kind "
        f'"{largest_missed_kind.lower()}".'
        if largest_missed
        else "",
        "## What the off-task rule adds",
        'Every row uses the same engine. "Rule off" sets `risk_ask_off_task` equal to '
        "`risk_ask`. For the tuned row the thresholds were fitted again under that constraint, "
        "with the same objective and the same recall floor.",
        "Golden test split:",
        table(OUTCOME_HEADERS, [outcome_row(label, on_test[label]) for label in rule_rows]),
        "InjecAgent-derived set:",
        table(OUTCOME_HEADERS, [outcome_row(label, on_outside[label]) for label in rule_rows]),
        f"![Recall against unnecessary holds for every configuration]({tradeoff_image})",
        "Each small point is one setting of `risk_ask`, `risk_ask_off_task` and `confidence`, "
        "measured on the golden test split."
        + (
            f" {out_of_frame} settings that held more than {pct(TRADEOFF_MAX_HELD)} of benign "
            "calls are outside the frame."
            if out_of_frame
            else ""
        ),
        "## How good each answer is",
        "Measured on the golden test cases that reached the model. AUROC is the chance that a "
        "true case scores above a false one; 1.0 is perfect and 0.5 is a coin flip.",
        table(
            [
                "Question",
                "AUROC",
                "Start threshold",
                "Precision",
                "Recall",
                "F1",
                "Tuned threshold",
                "Precision",
                "Recall",
                "F1",
                "True cases",
            ],
            question_rows,
        ),
        f"For `risk`, AUROC is {roc_auc_score(risk_labels, risk_scores):.3f} at telling "
        '"hard to undo or worse" from the rest. Mean score by true level:',
        table(
            ["True level", "Cases", "Mean score"],
            [
                [str(level), str(len(scores)), f"{statistics.mean(scores):.2f}"]
                for level, scores in sorted(risk_by_level.items())
            ],
        ),
        "## Calibration",
        f"Expected calibration error over {ECE_BINS} equal-width bins, on the golden test "
        "split. Zero means that when Jev says 70%, the answer is yes 70% of the time.",
        table(
            ["Question", "Expected calibration error"],
            [[f"`{question}`", f"{error:.3f}"] for question, (error, _) in calibration.items()],
        ),
        f"![Reliability diagram for the three yes/no questions]({reliability_image})",
        "## Does confidence predict mistakes?",
        "Each judged test case is given the lowest confidence among its answers. The cases are "
        "then decided on Jev's answers alone, most confident first, with no uncertainty band.",
        table(
            ["Share of calls decided", "Wrong decisions among them"],
            [[pct(share), pct(error_at(share))] for share in (0.5, 0.7, 0.8, 0.9, 1.0)],
        ),
        f"With every judged case decided, {len(wrong_at_full)} of {len(judged)} decisions were "
        f"wrong: {benign_held_at_full} benign calls held and "
        f"{len(wrong_at_full) - benign_held_at_full} risky calls allowed.",
        f"![Error rate as less confident answers are included]({coverage_image})",
        f"The curve starts at {pct(COVERAGE_FLOOR)} because the rate over fewer cases is too "
        "unstable to read.",
        "## Stability",
        f"{sampled} test cases were each asked five more times. The band chosen under the tuned "
        f"thresholds was the same all five times for {pct(agreement)} of them; under the "
        f"starting thresholds, for {pct(start_agreement)}; with only the ask levels tuned, "
        f"for {pct(ask_tuned_agreement)}. The table shows "
        "how far each answer moved between repeats of one request.",
        table(
            ["Answer", "Median spread", "95th percentile spread"],
            [
                [f"`{question}`", f"{median:.3f}", f"{high:.3f}"]
                for question, (median, high) in spreads.items()
            ],
        ),
        "## Speed and cost",
        table(
            ["Measure", "Value"],
            [
                ["Median time per decision", f"{percentile(timings, 0.5):.0f} ms"],
                ["95th percentile", f"{percentile(timings, 0.95):.0f} ms"],
                ["Slowest", f"{max(timings):.0f} ms"],
                ["Timed calls", str(len(timings))],
                [
                    "Input tokens per call, with the user's request",
                    f"{statistics.mean(tokens):.0f}",
                ],
                ["Input tokens per call, without it", f"{statistics.mean(tokens_bare):.0f}"],
                [
                    "Cost per 1,000 calls, with the user's request",
                    f"${statistics.mean(tokens) * 1000 * PRICE_PER_MILLION_TOKENS / 1e6:.3f}",
                ],
            ],
        ),
        "Times are for calls made one after another over one open connection, from the "
        "machine that ran the evaluation. A call on a new connection takes longer. Cost uses "
        f"the listed price of ${PRICE_PER_MILLION_TOKENS} per million input tokens.",
        "## What these numbers do not show",
        "- **The golden set may be easier than real traffic.** It was written by Claude, in "
        "clear language, with one call per case. Real agents produce messier calls.",
        "- **The labels are rules, not judgments.** A different rule for what counts as benign "
        "would move the unnecessary-hold rate. For example, a change the user asked for that is "
        "easy to undo counts as benign here.",
        "- **Each request was answered once.** Jev's answers vary slightly between identical "
        "requests, as the stability section shows; the cached answers are one draw.",
        "- **The external set's arguments were written for this evaluation.** InjecAgent "
        "publishes instructions and tool names, not the calls a hijacked agent would make.",
        "- **The off-task rule was chosen after seeing test results.** The first run of this "
        "evaluation showed, on the test split, that most unnecessary holds were edits the user "
        "had asked for, and the rule was added in response. Its thresholds were fitted on the "
        "train split only, but the test split is no longer untouched by design choices, so "
        "the gain it shows there is likely optimistic.",
        "- **The configuration that ships was picked by comparing these results.** Its "
        "thresholds were fitted on the train split, but the choice between configurations "
        "was made on the test sets, so its test numbers are likely optimistic.",
        "- **No adversarial pressure.** No case tries to talk the model into a wrong answer "
        "from inside the tool arguments.",
        "- **There is no LLM judge to compare with yet.** That comparison arrives with the "
        "next milestone.",
        "## Reproduce",
        "```text\nuv run python -m evals.report\n```",
        "This rebuilds the file and its figures from `evals/cache/`, with no network access "
        "and no API key. To ask the model again from scratch, delete the cache and run "
        "`uv run --env-file .env python -m evals.run` first.",
    ]
    return "\n\n".join(part for part in parts if part) + "\n"


def main() -> int:
    try:
        text = build()
    except MissingAnswers as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    RESULTS.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {RESULTS} and the figures in {IMAGES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
