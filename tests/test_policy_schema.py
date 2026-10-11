import copy
from pathlib import Path

import pytest
import yaml

from tollgate.policy.schema import PolicyError, load_policy, parse_policy

EXAMPLE = Path(__file__).resolve().parents[1] / "policy.example.yaml"


@pytest.fixture
def document() -> dict:
    return copy.deepcopy(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))


def test_the_example_policy_holds_the_fitted_ask_levels_and_the_spec_values_for_the_rest():
    policy = load_policy(EXAMPLE)

    assert policy.decision.timeout_ms == 800
    assert policy.thresholds.risk_ask == 1.4
    assert policy.thresholds.risk_ask_off_task == 0.2
    assert policy.thresholds.risk_block == 2.2
    assert policy.thresholds.confidence == 0.5
    assert {
        policy.thresholds.destructive,
        policy.thresholds.exfiltration,
        policy.thresholds.on_task,
    } == {0.5}
    assert policy.prefilter.path_sandbox.roots == ["examples/sandbox"]
    assert "private_key" in policy.prefilter.secret_patterns


@pytest.mark.parametrize(
    "missing",
    [
        ("thresholds", "destructive"),
        ("thresholds", "risk_ask_off_task"),
        ("thresholds", "confidence"),
        ("decision", "timeout_ms"),
        ("context", "max_chars"),
        ("prefilter", "max_argument_bytes"),
        ("approval", "timeout_seconds"),
        ("escalation", "enabled"),
        ("escalation", "timeout_ms"),
        ("result_scan", "injected"),
        ("result_scan", "max_chars"),
    ],
)
def test_a_policy_that_leaves_out_a_number_is_rejected(document, missing):
    """No number has a default in code."""
    section, field = missing
    del document[section][field]

    with pytest.raises(PolicyError, match=f"{section}.{field}: Field required"):
        parse_policy(document)


@pytest.mark.parametrize(
    "section",
    ["decision", "context", "thresholds", "prefilter", "approval", "escalation", "result_scan"],
)
def test_a_policy_that_leaves_out_a_section_is_rejected(document, section):
    del document[section]

    with pytest.raises(PolicyError, match=f"{section}: Field required"):
        parse_policy(document)


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    [
        ("thresholds", "destructive", 1.5, "thresholds.destructive"),
        ("thresholds", "risk_ask", 4, "thresholds.risk_ask"),
        ("thresholds", "risk_ask", 2.5, "risk_ask must not be above risk_block"),
        (
            "thresholds",
            "risk_ask_off_task",
            1.5,
            "risk_ask_off_task must not be above risk_ask",
        ),
        ("decision", "timeout_ms", 0, "decision.timeout_ms"),
        ("approval", "timeout_seconds", -1, "approval.timeout_seconds"),
        ("thresholds", "typo", 0.5, "thresholds.typo"),
    ],
    ids=[
        "probability>1",
        "risk>3",
        "ask>block",
        "off-task-ask>ask",
        "zero-timeout",
        "negative",
        "unknown-key",
    ],
)
def test_out_of_range_and_unknown_values_are_rejected(document, section, field, value, message):
    document[section][field] = value

    with pytest.raises(PolicyError, match=message):
        parse_policy(document)


def test_the_example_policy_scans_results_and_leaves_the_second_model_off():
    policy = load_policy(EXAMPLE)

    assert policy.escalation.enabled is False
    assert (policy.escalation.provider, policy.escalation.model) == ("gemini", "gemini-3.8-flash")
    assert policy.escalation.thinking == "low"
    assert policy.result_scan.enabled is True
    assert policy.result_scan.injected == 0.5


def test_the_second_model_can_be_another_provider_but_thinking_is_for_gemini(document):
    document["escalation"] |= {
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "thinking": None,
    }

    assert parse_policy(document).escalation.provider == "anthropic"

    document["escalation"]["thinking"] = "low"
    with pytest.raises(PolicyError, match="thinking can only be set when the provider is gemini"):
        parse_policy(document)

    document["escalation"] |= {"provider": "mistral", "thinking": None}
    with pytest.raises(PolicyError, match="escalation.provider"):
        parse_policy(document)


def test_a_secret_pattern_that_is_not_a_regular_expression_is_rejected(document):
    document["prefilter"]["secret_patterns"]["broken"] = "([unclosed"

    with pytest.raises(PolicyError, match="broken is not a valid regular expression"):
        parse_policy(document)


def test_a_tool_override_changes_only_the_thresholds_it_names(document):
    document["tools"] = {
        "write_file": {"thresholds": {"destructive": 0.3}},
        "send_email": {"thresholds": {"risk_ask_off_task": 0.1}},
    }

    policy = parse_policy(document)
    shared = policy.thresholds

    assert policy.thresholds_for("send_email").risk_ask_off_task == 0.1
    assert policy.thresholds_for("write_file").risk_ask_off_task == shared.risk_ask_off_task
    assert policy.thresholds_for("write_file").destructive == 0.3
    assert policy.thresholds_for("write_file").risk_ask == shared.risk_ask
    assert shared.destructive != 0.3
    assert shared.risk_ask_off_task != 0.1
    assert policy.thresholds_for("read_text_file") == shared


def test_a_tool_override_that_breaks_the_thresholds_is_rejected(document):
    document["tools"] = {"write_file": {"thresholds": {"risk_ask": 3.0}}}

    with pytest.raises(PolicyError, match="risk_ask must not be above risk_block"):
        parse_policy(document)

    document["tools"] = {"write_file": {"thresholds": {"risk_ask": 0.1}}}

    with pytest.raises(PolicyError, match="risk_ask_off_task must not be above risk_ask"):
        parse_policy(document)


def test_load_reports_a_missing_file_and_bad_yaml(tmp_path):
    with pytest.raises(PolicyError, match="cannot read the policy file"):
        load_policy(tmp_path / "absent.yaml")

    broken = tmp_path / "broken.yaml"
    broken.write_text("thresholds: [unclosed", encoding="utf-8")
    with pytest.raises(PolicyError, match="not valid YAML"):
        load_policy(broken)

    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(PolicyError, match="must be a mapping"):
        load_policy(empty)
