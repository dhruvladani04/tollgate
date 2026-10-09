import sqlite3
from datetime import UTC, datetime

from fakes import DECISION

from tollgate.audit.models import AuditRecord
from tollgate.audit.report import render
from tollgate.audit.store import AuditStore

RECORD = AuditRecord(
    timestamp=datetime(2026, 10, 9, 12, 30, 5, tzinfo=UTC),
    mode="observe",
    action="forwarded",
    upstream="filesystem",
    tool="write_file",
    arguments={"path": "notes.txt", "content": "héllo"},
    user_request="Save my notes",
    request_key="abc123",
    backend="jev",
    model="jev-1.13.0",
    cached=False,
    latency_ms=412.5,
    input_tokens=431,
    output_tokens=58,
    answers=DECISION.answers,
)
FAILED = RECORD.model_copy(
    update={
        "tool": "fetch",
        "model": None,
        "latency_ms": 10000.0,
        "input_tokens": None,
        "output_tokens": None,
        "answers": {},
        "error": "timeout: Request timed out.",
    }
)


def test_a_record_reads_back_exactly_as_it_was_written(tmp_path):
    with AuditStore(tmp_path / "audit.sqlite") as store:
        store.write(RECORD)

        assert store.recent(10) == [RECORD]


def test_each_write_adds_one_row_and_recent_returns_the_newest_first(tmp_path):
    with AuditStore(tmp_path / "audit.sqlite") as store:
        first = store.write(RECORD)
        second = store.write(FAILED)

        assert (first, second) == (1, 2)
        assert store.count() == 2
        assert [record.tool for record in store.recent(10)] == ["fetch", "write_file"]
        assert [record.tool for record in store.recent(1)] == ["fetch"]


def test_records_survive_closing_and_reopening_the_database(tmp_path):
    path = tmp_path / "nested" / "audit.sqlite"
    with AuditStore(path) as store:
        store.write(RECORD)

    with AuditStore(path) as reopened:
        assert reopened.recent(10) == [RECORD]


def test_probabilities_can_be_queried_with_plain_sql(tmp_path):
    path = tmp_path / "audit.sqlite"
    with AuditStore(path) as store:
        store.write(RECORD)

    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT json_extract(answers, '$.destructive.probability'),"
        " json_extract(answers, '$.risk.score'),"
        " json_extract(arguments, '$.path') FROM decisions"
    ).fetchone()
    connection.close()

    assert row == (0.87, 2.29, "notes.txt")


def test_the_printed_table_shows_what_the_milestone_asks_for():
    table = render([FAILED, RECORD])
    header, written, failed = table.splitlines()

    assert header.split() == [
        "time",
        "tool",
        "action",
        "model",
        "cached",
        "ms",
        "tokens",
        "answers",
    ]
    assert "write_file" in written
    assert "jev-1.13.0" in written
    assert "412" in written
    assert "431/58" in written
    assert "risk=2.29" in written
    assert "destructive=0.87" in written
    assert "no decision (timeout: Request timed out.)" in failed
