"""Escalation to a second judge, on made-up answers, and the committed judge answers."""

import json

from fakes import decision

from evals.data import all_cases, carries_secret, start_policy
from evals.judge import cache_path, key_for
from evals.judges import escalated_bands
from evals.tune import Sample
from tollgate.decide.cache import DecisionCache
from tollgate.decide.llm import JUDGES

START = start_policy().thresholds
CASE = {"risky": False, "malicious": False, "truth": {}}


def judged(**answers) -> Sample:
    return Sample(CASE, None, decision(**answers).answers)


CLEAR_READ = judged(risk=0.0, on_task=0.98, exfiltration=0.02)
# A destructive answer of 0.5 has no confidence, so the engine escalates.
UNSURE = judged(risk=0.0, destructive=0.5, on_task=0.98, exfiltration=0.02)
CLEAR_DELETE = judged(risk=2.8, destructive=0.97, on_task=0.98, exfiltration=0.02)


def test_only_a_call_the_first_model_leaves_uncertain_goes_to_the_judge():
    first = [CLEAR_READ, UNSURE, UNSURE, CLEAR_DELETE]
    # The judge disagrees on the two clear calls; that must not matter.
    second = [CLEAR_DELETE, CLEAR_READ, CLEAR_DELETE, CLEAR_READ]

    result, sent = escalated_bands(first, second, START)

    assert result == ["allow", "allow", "ask", "ask"]
    assert sent == 2


def test_a_call_both_models_are_unsure_about_stays_with_a_person():
    result, sent = escalated_bands([UNSURE], [UNSURE], START)

    assert result == ["escalate"]
    assert sent == 1


def test_a_call_the_judge_gave_no_answer_for_is_held_not_allowed():
    no_answer = Sample(CASE, "ask", None)

    result, _ = escalated_bands([UNSURE], [no_answer], START)

    assert result == ["ask"]


def test_an_exact_check_is_never_overruled_by_a_judge():
    blocked = Sample(CASE, "block", None)

    result, sent = escalated_bands([blocked], [blocked], START)

    assert result == ["block"]
    assert sent == 0


def test_the_committed_judge_answers_come_from_the_pinned_judges_and_hold_no_secret_case():
    policy = start_policy()
    secret_cases = [case for case in all_cases() if carries_secret(case, policy)]
    assert secret_cases

    for judge_key, judge in JUDGES.items():
        path = cache_path(judge_key)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines, judge_key
        for line in lines:
            entry = json.loads(line)
            # No request text is stored, so nothing a case contains can end up here.
            assert set(entry) == {"key", "decision"}
            assert entry["decision"]["model"] == judge.name
        cache = DecisionCache(path)
        for case in secret_cases:
            assert cache.get(key_for(judge, case)) is None, case["id"]
