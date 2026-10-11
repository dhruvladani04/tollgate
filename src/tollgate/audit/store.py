"""The audit log: one SQLite row for every tool call Tollgate judges."""

import json
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Self

from tollgate.audit.models import AuditRecord

# One column per field of an audit record.
_COLUMN_TYPES = {
    "timestamp": "TEXT NOT NULL",
    "mode": "TEXT NOT NULL",
    "band": "TEXT",
    "reasons": "TEXT",
    "action": "TEXT NOT NULL",
    "approval_id": "INTEGER",
    "upstream": "TEXT NOT NULL",
    "tool": "TEXT NOT NULL",
    "arguments": "TEXT",
    "user_request": "TEXT",
    "request_key": "TEXT",
    "backend": "TEXT",
    "model": "TEXT",
    "cached": "INTEGER NOT NULL",
    "latency_ms": "REAL",
    "input_tokens": "INTEGER",
    "output_tokens": "INTEGER",
    "answers": "TEXT NOT NULL",
    "error": "TEXT",
    "escalation_model": "TEXT",
    "escalation_answers": "TEXT",
    "escalation_latency_ms": "REAL",
    "escalation_error": "TEXT",
    "result_action": "TEXT",
    "injected": "REAL",
    "scan_latency_ms": "REAL",
    "withheld_result": "TEXT",
}
_COLUMNS = tuple(AuditRecord.model_fields)
assert set(_COLUMNS) == set(_COLUMN_TYPES), "every audit field needs a column type"
# These hold JSON, so SQLite's JSON functions can query them.
_JSON_COLUMNS = ("arguments", "answers", "reasons", "escalation_answers")
_CREATE = (
    "CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    + ", ".join(f"{name} {_COLUMN_TYPES[name]}" for name in _COLUMNS)
    + ")"
)
_INSERT = (
    f"INSERT INTO decisions ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join(':' + column for column in _COLUMNS)})"
)
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM decisions ORDER BY id DESC LIMIT ?"


class AuditStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute(_CREATE)
        self._add_missing_columns()
        self._connection.commit()

    def _add_missing_columns(self) -> None:
        """Bring a log written by an earlier version up to the current columns."""
        existing = {row[1] for row in self._connection.execute("PRAGMA table_info(decisions)")}
        for name in _COLUMNS:
            if name not in existing:
                # Every column added since the first version is optional.
                column_type = _COLUMN_TYPES[name].removesuffix(" NOT NULL")
                self._connection.execute(f"ALTER TABLE decisions ADD COLUMN {name} {column_type}")

    def write(self, record: AuditRecord) -> int:
        """Append one record and return its row id."""
        row = record.model_dump(mode="json")
        for column in _JSON_COLUMNS:
            if row[column] is not None:
                row[column] = json.dumps(row[column], ensure_ascii=False)
        cursor = self._connection.execute(_INSERT, row)
        self._connection.commit()
        return cursor.lastrowid or 0

    def annotate(self, row_id: int, **fields: object) -> None:
        """Add what became known after a record was written, such as its result scan."""
        unknown = set(fields) - set(_COLUMNS)
        if unknown:
            raise ValueError(f"not audit fields: {', '.join(sorted(unknown))}")
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        self._connection.execute(
            f"UPDATE decisions SET {assignments} WHERE id = :row_id", {**fields, "row_id": row_id}
        )
        self._connection.commit()

    def recent(self, limit: int) -> list[AuditRecord]:
        """The latest records, newest first."""
        records = []
        for values in self._connection.execute(_SELECT, (limit,)):
            # An empty column means the field keeps its default.
            row = {
                name: value
                for name, value in zip(_COLUMNS, values, strict=True)
                if value is not None
            }
            for column in _JSON_COLUMNS:
                if column in row:
                    row[column] = json.loads(row[column])
            records.append(AuditRecord.model_validate(row))
        return records

    def count(self) -> int:
        return self._connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]

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
