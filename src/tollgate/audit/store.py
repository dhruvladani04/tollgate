"""The audit log: one SQLite row for every tool call Tollgate asks a model about."""

import json
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Self

from tollgate.audit.models import AuditRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,
    mode          TEXT NOT NULL,
    action        TEXT NOT NULL,
    upstream      TEXT NOT NULL,
    tool          TEXT NOT NULL,
    arguments     TEXT,
    user_request  TEXT,
    request_key   TEXT,
    backend       TEXT,
    model         TEXT,
    cached        INTEGER NOT NULL,
    latency_ms    REAL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    answers       TEXT NOT NULL,
    error         TEXT
)
"""
_COLUMNS = tuple(AuditRecord.model_fields)
# `arguments` and `answers` hold JSON, so SQLite's JSON functions can query them.
_JSON_COLUMNS = ("arguments", "answers")
_INSERT = (
    f"INSERT INTO decisions ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join(':' + column for column in _COLUMNS)})"
)
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM decisions ORDER BY id DESC LIMIT ?"


class AuditStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute(_SCHEMA)
        self._connection.commit()

    def write(self, record: AuditRecord) -> int:
        """Append one record and return its row id."""
        row = record.model_dump(mode="json")
        for column in _JSON_COLUMNS:
            if row[column] is not None:
                row[column] = json.dumps(row[column], ensure_ascii=False)
        cursor = self._connection.execute(_INSERT, row)
        self._connection.commit()
        return cursor.lastrowid or 0

    def recent(self, limit: int) -> list[AuditRecord]:
        """The latest records, newest first."""
        records = []
        for values in self._connection.execute(_SELECT, (limit,)):
            row = dict(zip(_COLUMNS, values, strict=True))
            for column in _JSON_COLUMNS:
                if row[column] is not None:
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
