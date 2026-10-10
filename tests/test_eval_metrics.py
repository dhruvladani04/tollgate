import pytest

from evals.metrics import (
    best_setting,
    best_threshold,
    classify,
    expected_calibration_error,
    f_beta,
    flagged_f_beta,
    gate_outcome,
    percentile,
    risk_coverage,
)

GRID = [step / 20 for step in range(21)]


def test_f1_and_f2_on_a_worked_example():
    # 6 found, 2 false alarms, 4 missed: precision 0.75, recall 0.6.
    assert f_beta(6, 2, 4, 1.0) == pytest.approx(2 * 0.75 * 0.6 / (0.75 + 0.6))
    assert f_beta(6, 2, 4, 2.0) == pytest.approx(5 * 0.75 * 0.6 / (4 * 0.75 + 0.6))
    assert f_beta(0, 3, 4, 1.0) == 0.0


def test_classify_counts_precision_recall_and_f1():
    result = classify([True, True, False, False, True], [True, False, True, False, True])

    assert result.precision == pytest.approx(2 / 3)
    assert result.recall == pytest.approx(2 / 3)
    assert result.f1 == pytest.approx(2 / 3)
    assert result.positives == 3


def test_classify_with_nothing_predicted_or_nothing_true_is_zero_not_an_error():
    assert classify([False, False], [True, False]).precision == 0.0
    assert classify([True, False], [False, False]).recall == 0.0


def test_the_best_threshold_separates_the_classes():
    scores = [0.05, 0.1, 0.2, 0.7, 0.8, 0.9]
    actual = [False, False, False, True, True, True]

    threshold = best_threshold(scores, actual, candidates=GRID)

    assert 0.2 <= threshold < 0.7


def test_a_low_scoring_positive_pulls_the_threshold_below_one_half():
    """The case the vendor's benchmark describes: 0.5 is the wrong default."""
    scores = [0.02, 0.03, 0.05, 0.3, 0.35, 0.9]
    actual = [False, False, False, True, True, True]

    threshold = best_threshold(scores, actual, candidates=GRID)

    assert threshold < 0.3
    assert classify([s > threshold for s in scores], actual).recall == 1.0
    assert classify([s > 0.5 for s in scores], actual).recall == pytest.approx(1 / 3)


def test_a_threshold_can_point_downward_for_scores_where_low_means_yes():
    on_task = [0.95, 0.9, 0.8, 0.2, 0.1]
    off_task = [False, False, False, True, True]

    threshold = best_threshold(on_task, off_task, candidates=GRID, above=False)

    assert 0.2 < threshold <= 0.8
    assert [score < threshold for score in on_task] == off_task


def test_a_higher_beta_buys_recall_with_a_lower_threshold():
    scores = [0.1, 0.2, 0.3, 0.4, 0.45, 0.6, 0.7, 0.9]
    actual = [False, True, False, False, True, False, True, True]

    balanced = best_threshold(scores, actual, candidates=GRID, beta=1.0)
    recall_first = best_threshold(scores, actual, candidates=GRID, beta=3.0)

    assert recall_first <= balanced


def test_best_setting_returns_the_first_of_equal_candidates():
    assert best_setting([0.0, 0.5, 1.0], lambda value: -abs(value - 0.5)) == 0.5
    assert best_setting([0.0, 0.5, 1.0], lambda value: 1.0) == 0.0


def test_a_perfectly_calibrated_model_has_no_calibration_error():
    probabilities = [0.25] * 4 + [0.75] * 4
    actual = [True, False, False, False, True, True, True, False]

    error, table = expected_calibration_error(probabilities, actual, bins=4)

    assert error == pytest.approx(0.0)
    assert table == [(0.25, 0.25, 4), (0.75, 0.75, 4)]


def test_an_overconfident_model_shows_its_gap():
    error, _ = expected_calibration_error([0.9] * 10, [True] * 5 + [False] * 5, bins=15)

    assert error == pytest.approx(0.4)


def test_a_probability_of_exactly_one_lands_in_the_last_bin():
    error, table = expected_calibration_error([1.0, 1.0], [True, True], bins=15)

    assert error == pytest.approx(0.0)
    assert table == [(1.0, 1.0, 2)]


def test_gate_outcome_on_a_worked_example():
    bands = ["allow", "ask", "block", "escalate", "allow", "block", "ask", "allow"]
    risky = [False, False, True, True, True, True, True, False]
    malicious = [False, False, True, False, False, False, True, False]

    outcome = gate_outcome(bands, risky, malicious)

    assert (outcome.cases, outcome.risky, outcome.benign) == (8, 5, 3)
    assert outcome.recall_on_risky == pytest.approx(4 / 5)
    assert outcome.false_ask_rate == pytest.approx(1 / 3)
    assert outcome.block_precision == pytest.approx(1 / 2)
    assert outcome.block_recall == pytest.approx(1 / 2)
    assert outcome.share_escalated == pytest.approx(1 / 8)
    assert outcome.share_blocked == pytest.approx(2 / 8)
    assert outcome.share_held == pytest.approx(3 / 8)


def test_gate_outcome_with_nothing_blocked_reports_zero_not_an_error():
    outcome = gate_outcome(["allow", "ask"], [False, True], [False, True])

    assert outcome.block_precision == 0.0
    assert outcome.block_recall == 0.0


def test_flagged_f_beta_treats_every_non_allow_band_as_a_flag():
    bands = ["allow", "ask", "block", "escalate"]
    risky = [True, True, True, False]

    assert flagged_f_beta(bands, risky, 1.0) == pytest.approx(f_beta(2, 1, 1, 1.0))


def test_risk_coverage_drops_the_least_confident_cases_first():
    confidences = [0.9, 0.8, 0.2, 0.1]
    correct = [True, True, False, True]

    points = risk_coverage(confidences, correct)

    assert points == [(0.25, 0.0), (0.5, 0.0), (0.75, pytest.approx(1 / 3)), (1.0, 0.25)]


def test_percentile_interpolates_between_values():
    values = [10, 20, 30, 40, 50]

    assert percentile(values, 0.5) == 30
    assert percentile(values, 0.95) == pytest.approx(48)
    assert percentile([7], 0.95) == 7
