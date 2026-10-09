"""The approval queue: calls held for a person.

The proxy puts a held call here and waits. A person answers from another
process, such as the `tollgate-approve` command. The two share one SQLite file.
"""

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, Self

APPROVALS_FILE = "approvals.sqlite"
# How often the proxy looks for an answer to a held call.
POLL_SECONDS = 0.25

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created      TEXT NOT NULL,
    expires      TEXT NOT NULL,
    upstream     TEXT NOT NULL,
    tool         TEXT NOT NULL,
    arguments    TEXT,
    user_request TEXT,
    reasons      TEXT NOT NULL,
    status       TEXT NOT NULL,
    decided      TEXT
)
"""
_COLUMNS = "id, created, expires, upstream, tool, arguments, user_request, reasons, status"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalError(Exception):
    """A held call could not be answered: it is unknown, already answered or expired."""


@dataclass(frozen=True)
class HeldCall:
    id: int
    created: datetime
    expires: datetime
    upstream: str
    tool: str
    arguments: dict[str, Any] | None
    user_request: str | None
    reasons: tuple[str, ...]
    status: ApprovalStatus


class ApprovalQueue:
    def __init__(self, path: Path, poll_seconds: float = POLL_SECONDS) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute(_SCHEMA)
        self._connection.commit()
        self._poll_seconds = poll_seconds

    def submit(
        self,
        *,
        upstream: str,
        tool: str,
        arguments: dict[str, Any] | None,
        user_request: str | None,
        reasons: Sequence[str],
        timeout_seconds: float,
    ) -> int:
        """Hold a call and return its id. It expires if nobody answers in time."""
        now = datetime.now(UTC)
        cursor = self._connection.execute(
            "INSERT INTO approvals (created, expires, upstream, tool, arguments, user_request,"
            " reasons, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now.isoformat(),
                (now + timedelta(seconds=timeout_seconds)).isoformat(),
                upstream,
                tool,
                None if arguments is None else json.dumps(arguments, ensure_ascii=False),
                user_request,
                json.dumps(list(reasons), ensure_ascii=False),
                ApprovalStatus.PENDING,
            ),
        )
        self._connection.commit()
        return cursor.lastrowid or 0

    def get(self, call_id: int) -> HeldCall | None:
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE id = ?", (call_id,)
        ).fetchone()
        return None if row is None else _held_call(row)

    def pending(self) -> list[HeldCall]:
        """Calls still waiting for an answer, oldest first."""
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE status = ? AND expires > ? ORDER BY id",
            (ApprovalStatus.PENDING, datetime.now(UTC).isoformat()),
        ).fetchall()
        return [_held_call(row) for row in rows]

    def answer(self, call_id: int, approve: bool) -> HeldCall:
        """Approve or deny a held call. Only a call that is still waiting can be answered."""
        status = ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED
        now = datetime.now(UTC).isoformat()
        cursor = self._connection.execute(
            "UPDATE approvals SET status = ?, decided = ?"
            " WHERE id = ? AND status = ? AND expires > ?",
            (status, now, call_id, ApprovalStatus.PENDING, now),
        )
        self._connection.commit()
        held = self.get(call_id)
        if held is None:
            raise ApprovalError(f"there is no held call #{call_id}")
        if cursor.rowcount == 0:
            state = "expired" if held.status is ApprovalStatus.PENDING else held.status
            raise ApprovalError(f"held call #{call_id} is no longer waiting: it is {state}")
        return held

    def close_out(self, call_id: int, status: ApprovalStatus) -> None:
        """Mark a call that is still waiting as expired or cancelled."""
        self._connection.execute(
            "UPDATE approvals SET status = ?, decided = ? WHERE id = ? AND status = ?",
            (status, datetime.now(UTC).isoformat(), call_id, ApprovalStatus.PENDING),
        )
        self._connection.commit()

    async def wait(self, call_id: int) -> ApprovalStatus:
        """Wait until a held call is answered or runs out of time; return how it ended."""
        while True:
            held = self.get(call_id)
            if held is None:
                return ApprovalStatus.EXPIRED
            if held.status is not ApprovalStatus.PENDING:
                return held.status
            if datetime.now(UTC) >= held.expires:
                self.close_out(call_id, ApprovalStatus.EXPIRED)
                return ApprovalStatus.EXPIRED
            await asyncio.sleep(self._poll_seconds)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _held_call(row: tuple[Any, ...]) -> HeldCall:
    call_id, created, expires, upstream, tool, arguments, user_request, reasons, status = row
    return HeldCall(
        id=call_id,
        created=datetime.fromisoformat(created),
        expires=datetime.fromisoformat(expires),
        upstream=upstream,
        tool=tool,
        arguments=None if arguments is None else json.loads(arguments),
        user_request=user_request,
        reasons=tuple(json.loads(reasons)),
        status=ApprovalStatus(status),
    )
