"""The pre-filter: checks code can decide exactly, which never reach the model."""

import sys

import pytest

from tollgate.policy.engine import Band
from tollgate.policy.prefilter import prefilter, redact
from tollgate.policy.schema import PathSandbox, Prefilter

# Built at run time so that no token-shaped literal sits in the repository.
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"

RULES = Prefilter(
    allow_tools=["list_allowed_directories"],
    deny_tools=["delete_everything"],
    max_argument_bytes=200,
    secret_patterns={
        "aws_access_key_id": r"\bAKIA[0-9A-Z]{16}\b",
        "private_key": (
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
            r"(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)?"
        ),
    },
    path_sandbox=PathSandbox(roots=["sandbox"], arguments=["path", "paths", "destination"]),
)


@pytest.fixture
def cwd(tmp_path):
    (tmp_path / "sandbox").mkdir()
    return tmp_path


def check(tool, arguments, cwd, rules=RULES):
    return prefilter(tool, arguments, rules, cwd)


def test_an_ordinary_call_is_left_for_the_model(cwd):
    assert check("read_text_file", {"path": "sandbox/notes.txt"}, cwd) is None
    assert check("fetch", {"url": "https://example.com"}, cwd) is None
    assert check("ping", None, cwd) is None


def test_a_denied_tool_is_blocked_by_name(cwd):
    verdict = check("delete_everything", {}, cwd)

    assert verdict.band is Band.BLOCK
    assert verdict.source == "prefilter"
    assert "delete_everything" in verdict.reasons[0]


def test_an_allowed_tool_skips_the_model(cwd):
    verdict = check("list_allowed_directories", {}, cwd)

    assert verdict.band is Band.ALLOW
    assert verdict.source == "prefilter"


@pytest.mark.parametrize(
    "arguments",
    [
        {"content": f"aws_key = {AWS_KEY}"},
        {"headers": {"X-Key": [f"prefix {AWS_KEY} suffix"]}},
        {"content": PRIVATE_KEY},
    ],
    ids=["top-level", "nested", "private-key"],
)
def test_a_secret_anywhere_in_the_arguments_is_blocked(cwd, arguments):
    verdict = check("write_note", arguments, cwd)

    assert verdict.band is Band.BLOCK
    assert "secret" in verdict.reasons[0]


def test_the_reason_names_the_pattern_and_never_the_secret(cwd):
    verdict = check("write_note", {"content": f"key {AWS_KEY}"}, cwd)

    assert "aws_access_key_id" in verdict.reasons[0]
    assert AWS_KEY not in " ".join(verdict.reasons)


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "sandbox/notes.txt"},
        {"path": "sandbox/deep/er/notes.txt"},
        {"paths": ["sandbox/a.txt", "sandbox/b.txt"]},
        {"path": "sandbox"},
    ],
    ids=["file", "nested", "list", "the-root-itself"],
)
def test_a_path_inside_the_sandbox_passes(cwd, arguments):
    assert check("read_text_file", arguments, cwd) is None


def test_an_absolute_path_inside_the_sandbox_passes(cwd):
    assert check("read_text_file", {"path": str(cwd / "sandbox" / "notes.txt")}, cwd) is None


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "secrets.txt"},
        {"path": "sandbox/../secrets.txt"},
        {"path": "sandbox_other/notes.txt"},
        {"paths": ["sandbox/a.txt", "../outside.txt"]},
        {"path": "sandbox/a.txt", "destination": "elsewhere/a.txt"},
    ],
    ids=["sibling", "dot-dot", "prefix-lookalike", "one-of-a-list", "second-path-argument"],
)
def test_a_path_outside_the_sandbox_is_blocked(cwd, arguments):
    verdict = check("move_file", arguments, cwd)

    assert verdict.band is Band.BLOCK
    assert "outside" in verdict.reasons[0]


def test_an_absolute_path_outside_the_sandbox_is_blocked(cwd):
    verdict = check("read_text_file", {"path": str(cwd / "id_rsa")}, cwd)

    assert verdict.band is Band.BLOCK


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths ignore case")
def test_on_windows_a_path_that_differs_only_in_case_is_inside(cwd):
    assert check("read_text_file", {"path": "SANDBOX/Notes.txt"}, cwd) is None


def test_arguments_that_are_not_path_arguments_are_not_checked_as_paths(cwd):
    assert check("fetch", {"url": "../outside.txt", "note": "/etc/passwd"}, cwd) is None


def test_with_no_roots_the_sandbox_check_is_off(cwd):
    rules = RULES.model_copy(update={"path_sandbox": PathSandbox(roots=[], arguments=["path"])})

    assert check("read_text_file", {"path": "/etc/passwd"}, cwd, rules) is None


def test_arguments_too_large_for_the_model_are_held_for_a_person(cwd):
    verdict = check("write_note", {"content": "x" * 300}, cwd)

    assert verdict.band is Band.ASK
    assert "200" in verdict.reasons[0]


def test_the_exact_checks_apply_in_order_of_severity(cwd):
    secret_in_denied = check("delete_everything", {"content": AWS_KEY}, cwd)
    secret_in_allowed = check("list_allowed_directories", {"content": AWS_KEY}, cwd)
    outside_in_allowed = check("list_allowed_directories", {"path": "../x"}, cwd)
    large_in_allowed = check("list_allowed_directories", {"content": "x" * 300}, cwd)

    assert "deny list" in secret_in_denied.reasons[0]
    assert secret_in_allowed.band is Band.BLOCK
    assert outside_in_allowed.band is Band.BLOCK
    assert large_in_allowed.band is Band.ALLOW


def test_redaction_replaces_each_secret_and_leaves_everything_else():
    arguments = {
        "content": f"first {AWS_KEY} then text",
        "nested": {"list": [PRIVATE_KEY, 7, None, "plain"]},
        "count": 3,
    }

    redacted = redact(arguments, RULES)

    assert redacted == {
        "content": "first [redacted: aws_access_key_id] then text",
        "nested": {"list": ["[redacted: private_key]", 7, None, "plain"]},
        "count": 3,
    }
    assert arguments["content"].startswith("first AKIA")


def test_redaction_of_no_arguments_is_no_arguments():
    assert redact(None, RULES) is None
