import copy
from pathlib import Path

import pytest
import yaml

from tollgate.policy.schema import PolicyError, load_policy, parse_policy

EXAMPLE = Path(__file__).resolve().parents[1] / "policy.example.yaml"


@pytest.fixture
def document() -> dict:
    return copy.deepcopy(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))


def test_the_example_policy_holds_the_starting_values_from_the_spec():
    policy = load_policy(EXAMPLE)

    assert policy.decision.timeout_ms == 800
    assert policy.thresholds.risk_ask == 1.0
    assert policy.thresholds.risk_ask_off_task == 0.5
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
    ],
)
def test_a_policy_that_leaves_out_a_number_is_rejected(document, missing):
    """No number has a default in code."""
    section, field = missing
    del document[section][field]

    with pytest.raises(PolicyError, match=f"{section}.{field}: Field required"):
        parse_policy(document)


@pytest.mark.parametrize("section", ["decision", "context", "thresholds", "prefilter", "approval"])
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


def test_a_secret_pattern_that_is_not_a_regular_expression_is_rejected(document):
    document["prefilter"]["secret_patterns"]["broken"] = "([unclosed"

    with pytest.raises(PolicyError, match="broken is not a valid regular expression"):
        parse_policy(document)


def test_a_tool_override_changes_only_the_thresholds_it_names(document):
    document["tools"] = {
        "write_file": {"thresholds": {"destructive": 0.3}},
        "send_email": {"thresholds": {"risk_ask_off_task": 0.2}},
    }

    policy = parse_policy(document)

    assert policy.thresholds_for("send_email").risk_ask_off_task == 0.2
    assert policy.thresholds_for("write_file").risk_ask_off_task == 0.5
    assert policy.thresholds_for("write_file").destructive == 0.3
    assert policy.thresholds_for("write_file").risk_ask == 1.0
    assert policy.thresholds_for("read_text_file") == policy.thresholds


def test_a_tool_override_that_breaks_the_thresholds_is_rejected(document):
    document["tools"] = {"write_file": {"thresholds": {"risk_ask": 3.0}}}

    with pytest.raises(PolicyError, match="risk_ask must not be above risk_block"):
        parse_policy(document)

    document["tools"] = {"write_file": {"thresholds": {"risk_ask": 0.2}}}

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
