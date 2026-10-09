import asyncio

import pytest

from tollgate.approve.cli import main, render
from tollgate.approve.queue import APPROVALS_FILE, ApprovalError, ApprovalQueue, ApprovalStatus

CALL = {
    "upstream": "filesystem",
    "tool": "write_file",
    "arguments": {"path": "notes.txt", "content": "héllo"},
    "user_request": "Tidy my notes",
    "reasons": ["destructive: the call deletes or overwrites data (0.91 is above 0.50)"],
}


@pytest.fixture
def queue(tmp_path):
    with ApprovalQueue(tmp_path / APPROVALS_FILE, poll_seconds=0.01) as opened:
        yield opened


def test_a_submitted_call_waits_with_everything_a_person_needs_to_decide(queue):
    call_id = queue.submit(**CALL, timeout_seconds=60)

    [held] = queue.pending()
    assert held.id == call_id
    assert held.status is ApprovalStatus.PENDING
    assert (held.upstream, held.tool, held.arguments) == (
        "filesystem",
        "write_file",
        {"path": "notes.txt", "content": "héllo"},
    )
    assert held.user_request == "Tidy my notes"
    assert held.reasons == tuple(CALL["reasons"])
    assert (held.expires - held.created).total_seconds() == pytest.approx(60)


@pytest.mark.parametrize(
    ("approve", "status"),
    [(True, ApprovalStatus.APPROVED), (False, ApprovalStatus.DENIED)],
)
def test_an_answer_from_another_connection_ends_the_wait(tmp_path, queue, approve, status):
    call_id = queue.submit(**CALL, timeout_seconds=60)

    async def scenario():
        waiting = asyncio.create_task(queue.wait(call_id))
        await asyncio.sleep(0.05)
        assert not waiting.done()
        with ApprovalQueue(tmp_path / APPROVALS_FILE) as elsewhere:
            elsewhere.answer(call_id, approve=approve)
        return await asyncio.wait_for(waiting, 5)

    assert asyncio.run(scenario()) is status
    assert queue.pending() == []


def test_a_call_nobody_answers_expires(queue):
    call_id = queue.submit(**CALL, timeout_seconds=0.05)

    status = asyncio.run(asyncio.wait_for(queue.wait(call_id), 5))

    assert status is ApprovalStatus.EXPIRED
    assert queue.get(call_id).status is ApprovalStatus.EXPIRED


def test_a_call_can_be_answered_only_once_and_only_while_it_waits(queue):
    answered = queue.submit(**CALL, timeout_seconds=60)
    expired = queue.submit(**CALL, timeout_seconds=0)
    queue.answer(answered, approve=False)

    with pytest.raises(ApprovalError, match="it is denied"):
        queue.answer(answered, approve=True)
    with pytest.raises(ApprovalError, match="it is expired"):
        queue.answer(expired, approve=True)
    with pytest.raises(ApprovalError, match="no held call #99"):
        queue.answer(99, approve=True)

    assert queue.get(answered).status is ApprovalStatus.DENIED


def test_closing_out_does_not_overwrite_an_answer(queue):
    call_id = queue.submit(**CALL, timeout_seconds=60)
    queue.answer(call_id, approve=True)

    queue.close_out(call_id, ApprovalStatus.CANCELLED)

    assert queue.get(call_id).status is ApprovalStatus.APPROVED


def test_the_listing_shows_the_call_the_reason_and_the_time_left(queue):
    queue.submit(**CALL, timeout_seconds=60)

    listing = render(queue.pending())

    assert "#1  write_file  (from filesystem)" in listing
    assert "why: destructive" in listing
    assert '"path": "notes.txt"' in listing
    assert "user asked: Tidy my notes" in listing
    assert "s left" in listing
    assert render([]) == "nothing is waiting for approval"


def test_long_arguments_are_cut_off_in_the_listing(queue):
    queue.submit(**(CALL | {"arguments": {"content": "x" * 5000}}), timeout_seconds=60)

    listing = render(queue.pending())

    assert len(listing) < 1000
    assert " ..." in listing


def test_the_command_lists_approves_and_denies(tmp_path, queue, capsys):
    first = queue.submit(**CALL, timeout_seconds=60)
    second = queue.submit(**CALL, timeout_seconds=60)
    state = ["--state-dir", str(tmp_path)]

    assert main([*state, "list"]) == 0
    assert f"#{first}  write_file" in capsys.readouterr().out
    assert main([*state, "approve", str(first)]) == 0
    assert f"approved #{first} (write_file)" in capsys.readouterr().out
    assert main([*state, "deny", str(second)]) == 0
    assert f"denied #{second} (write_file)" in capsys.readouterr().out

    assert queue.get(first).status is ApprovalStatus.APPROVED
    assert queue.get(second).status is ApprovalStatus.DENIED
    assert main([*state, "list"]) == 0
    assert "nothing is waiting" in capsys.readouterr().out


def test_the_command_reports_a_call_it_cannot_answer(tmp_path, queue, capsys):
    call_id = queue.submit(**CALL, timeout_seconds=60)
    queue.answer(call_id, approve=True)

    assert main(["--state-dir", str(tmp_path), "deny", str(call_id)]) == 1
    assert "no longer waiting" in capsys.readouterr().err


def test_the_command_works_before_any_call_was_ever_held(tmp_path, capsys):
    empty = ["--state-dir", str(tmp_path / "fresh")]

    assert main([*empty, "list"]) == 0
    assert "nothing is waiting" in capsys.readouterr().out
    assert main([*empty, "approve", "1"]) == 1
    assert "no approval queue" in capsys.readouterr().err
    assert not (tmp_path / "fresh").exists()
