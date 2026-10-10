"""The Claude Code hook that tells Tollgate what the user asked for."""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from tollgate.context import CallContext
from tollgate.prompts import PROMPTS_FILE, PromptLog, record_hook_event

START = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return START + timedelta(minutes=minutes)


def event(prompt="Tidy my notes", **changes) -> bytes:
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-1",
        "cwd": "unused",
        "prompt": prompt,
    } | changes
    return json.dumps(payload).encode("utf-8")


@pytest.fixture
def log(tmp_path):
    return PromptLog(tmp_path / PROMPTS_FILE)


def test_the_latest_prompt_is_the_request_and_earlier_ones_are_its_context(log):
    log.record("s", "Find the merged branches", at(1))
    log.record("s", "Which of them are safe to delete?", at(2))
    log.record("s", "Yes, delete those", at(3))

    assert log.context(since=START) == CallContext(
        user_request="Yes, delete those",
        recent_context=("Find the merged branches", "Which of them are safe to delete?"),
    )


def test_only_the_request_and_two_turns_of_context_are_kept(log):
    for minute, text in enumerate(["one", "two", "three", "four"], 1):
        log.record("s", text, at(minute))

    assert log.context(since=START) == CallContext(
        user_request="four", recent_context=("two", "three")
    )


def test_a_new_session_does_not_inherit_the_old_sessions_prompts(log):
    log.record("old", "Delete the staging database", at(1))
    log.record("new", "Read the README", at(2))

    assert log.context(since=START) == CallContext(user_request="Read the README")


def test_prompts_from_before_this_process_started_are_not_context(log):
    log.record("s", "An old request", at(1))
    log.record("s", "A newer one", at(5))

    assert log.context(since=at(3)) == CallContext(user_request="A newer one")
    assert log.context(since=at(10)) is None


def test_no_file_or_a_broken_file_means_no_context(tmp_path, log):
    assert log.context(since=START) is None

    (tmp_path / PROMPTS_FILE).write_text("{not json", encoding="utf-8")

    assert log.context(since=START) is None
    log.record("s", "Still works after a broken file", at(1))
    assert log.context(since=START).user_request == "Still works after a broken file"


def test_the_hook_records_the_prompt_in_the_projects_state_folder(tmp_path):
    record_hook_event([], event("Résumé the notes → done"), {"CLAUDE_PROJECT_DIR": str(tmp_path)})

    context = PromptLog(tmp_path / ".tollgate" / PROMPTS_FILE).context(since=START)

    assert context == CallContext(user_request="Résumé the notes → done")


def test_without_a_project_variable_the_hook_uses_the_sessions_folder(tmp_path):
    record_hook_event([], event(cwd=str(tmp_path)), {})

    assert (tmp_path / ".tollgate" / PROMPTS_FILE).exists()


def test_the_hook_can_be_pointed_at_another_state_folder(tmp_path):
    record_hook_event(["--state-dir", "elsewhere"], event(), {"CLAUDE_PROJECT_DIR": str(tmp_path)})

    assert (tmp_path / "elsewhere" / PROMPTS_FILE).exists()


@pytest.mark.parametrize(
    "payload",
    [
        event(hook_event_name="PreToolUse"),
        event(agent_id="subagent-7"),
        event(prompt="   "),
        event(prompt=None),
    ],
    ids=["another-event", "inside-a-subagent", "blank", "no-prompt"],
)
def test_the_hook_ignores_anything_that_is_not_a_user_prompt(tmp_path, payload):
    record_hook_event([], payload, {"CLAUDE_PROJECT_DIR": str(tmp_path)})

    assert not (tmp_path / ".tollgate").exists()


def run_hook(payload: bytes, *arguments: str, cwd) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "tollgate.prompts", *arguments],
        input=payload,
        capture_output=True,
        cwd=cwd,
        timeout=60,
    )


def test_the_command_records_a_prompt_and_prints_nothing_for_the_model_to_read(tmp_path):
    finished = run_hook(event("Tidy my notes", cwd=str(tmp_path)), cwd=tmp_path)

    assert finished.returncode == 0
    assert finished.stdout == b""
    context = PromptLog(tmp_path / ".tollgate" / PROMPTS_FILE).context(since=START)
    assert context.user_request == "Tidy my notes"


@pytest.mark.parametrize(
    ("payload", "arguments"),
    [(b"not json at all", ()), (event(), ("--bogus",)), (event(), ("--state-dir",))],
    ids=["bad-input", "unknown-option", "missing-value"],
)
def test_a_failing_command_never_blocks_the_prompt(tmp_path, payload, arguments):
    """Exit status 2 would make Claude Code refuse the user's prompt."""
    finished = run_hook(payload, *arguments, cwd=tmp_path)

    assert finished.returncode == 1
    assert finished.stdout == b""
    assert finished.stderr.startswith(b"tollgate-hook: ")
