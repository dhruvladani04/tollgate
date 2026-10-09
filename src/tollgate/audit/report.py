"""Plain-text rendering of audit records."""

from tollgate.audit.models import AuditRecord
from tollgate.decide.base import NoulAnswer


def render(records: list[AuditRecord]) -> str:
    """One line per record, oldest first: the band, what happened, and the evidence."""
    rows = [("time", "tool", "band", "action", "model", "cached", "ms", "tokens", "evidence")]
    for record in reversed(records):
        answers = "  ".join(
            f"{name}={answer.probability if isinstance(answer, NoulAnswer) else answer.score:.2f}"
            for name, answer in record.answers.items()
        )
        # With no answers, the reason says what decided: an exact check or a failure.
        evidence = answers or "; ".join(record.reasons) or f"no decision ({record.error})"
        latency = "" if record.latency_ms is None else f"{record.latency_ms:.0f}"
        tokens = (
            "" if record.input_tokens is None else f"{record.input_tokens}/{record.output_tokens}"
        )
        rows.append(
            (
                record.timestamp.astimezone().strftime("%H:%M:%S"),
                record.tool,
                record.band or "",
                record.action,
                record.model or "",
                "yes" if record.cached else "no",
                latency,
                tokens,
                evidence,
            )
        )
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]) - 1)]
    return "\n".join(
        "  ".join([*(cell.ljust(width) for cell, width in zip(row, widths, strict=False)), row[-1]])
        for row in rows
    )
