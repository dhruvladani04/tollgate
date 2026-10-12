"""`tollgate-dashboard`: a local web app over Tollgate's own files.

It has two parts. This module is a small JSON API that reads the audit log and
the approval queue the proxy writes, and the policy file. The pages are a
Next.js app in the repo's `dashboard/` folder, built to static files that this
module serves. So the dashboard needs nothing from the running proxy, and
running it needs no Node.

It is meant for one person on their own machine: it has no login and listens
on localhost.
"""

import argparse
import hmac
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from tollgate.approve.prompt import preview_arguments
from tollgate.approve.queue import APPROVALS_FILE, ApprovalError, ApprovalQueue
from tollgate.audit.models import AuditRecord
from tollgate.audit.store import AuditStore
from tollgate.decide.base import NoulAnswer
from tollgate.policy.schema import RISK_TOP, Policy, PolicyError, Thresholds, load_policy

AUDIT_FILE = "audit.sqlite"
# The built pages. `npm run build` in the repo's dashboard/ folder writes them here.
STATIC = Path(__file__).parent / "static"
# How many of the latest calls a page looks at, unless it asks otherwise.
DEFAULT_CALLS = 200
MOST_CALLS = 1000

# What each threshold is compared with, in which direction a call crosses it, and
# what crossing it does.
THRESHOLD_NOTES = {
    "risk_ask": ("risk", "at or above", "A person is asked."),
    "risk_ask_off_task": (
        "risk",
        "at or above",
        "A person is asked, if the call is off task or came with no user request.",
    ),
    "risk_block": ("risk", "above", "The call is blocked, if it is also off task."),
    "destructive": ("destructive", "above", "A person is asked."),
    "exfiltration": ("exfiltration", "above", "The call is blocked."),
    "on_task": ("on_task", "below", "The call counts as off task."),
}


@dataclass(frozen=True)
class Row:
    """One audit record, with the words the dashboard shows for it."""

    record: AuditRecord
    outcome: str
    """What happened, in one phrase a person can scan for."""
    kind: str
    """The group that outcome belongs to: ok, held, stopped or warned."""
    scores: dict[str, float]


def describe(record: AuditRecord) -> Row:
    if record.action == "blocked":
        outcome, kind = "Blocked", "stopped"
    elif record.action == "denied":
        outcome, kind = "Denied by a person", "stopped"
    elif record.action == "expired":
        outcome, kind = "Held, nobody answered", "stopped"
    elif record.result_action == "withheld":
        if record.mode == "observe":
            outcome, kind = "Ran; result would be withheld", "warned"
        else:
            outcome, kind = "Ran; result withheld", "stopped"
    elif record.approval_id is not None:
        outcome, kind = "Approved by a person", "held"
    elif record.mode == "observe" and record.band not in (None, "allow"):
        outcome, kind = f"Ran; enforce mode would {record.band}", "warned"
    elif record.result_action == "unscanned":
        outcome, kind = "Allowed; result not scanned", "warned"
    else:
        outcome, kind = "Allowed", "ok"
    scores = {
        name: answer.probability if isinstance(answer, NoulAnswer) else answer.score
        for name, answer in record.answers.items()
    }
    return Row(record, outcome, kind, scores)


def as_json(row: Row) -> dict[str, Any]:
    record = row.record
    return {
        "time": record.timestamp.isoformat(),
        "tool": record.tool,
        "upstream": record.upstream,
        "mode": record.mode,
        "band": record.band,
        "outcome": row.outcome,
        "kind": row.kind,
        "reasons": record.reasons,
        "error": record.error,
        "approval_id": record.approval_id,
        "user_request": record.user_request,
        "arguments": preview_arguments(record.arguments),
        "scores": row.scores,
        "model": record.model,
        "cached": record.cached,
        "latency_ms": record.latency_ms,
        "second_model": record.escalation_model,
        "second_model_error": record.escalation_error,
        "result_action": record.result_action,
        "injected": record.injected,
        "withheld_result": record.withheld_result,
    }


def crossed(name: str, score: float, thresholds: Thresholds) -> bool:
    _, direction, _ = THRESHOLD_NOTES[name]
    limit = getattr(thresholds, name)
    if direction == "above":
        return score > limit
    if direction == "below":
        return score < limit
    return score >= limit


def threshold_rows(policy: Policy, rows: list[Row]) -> list[dict[str, Any]]:
    """For each threshold: its value, the recent scores, how many crossed, and the nearest miss."""
    result = []
    for name, (answer, direction, effect) in THRESHOLD_NOTES.items():
        limit = getattr(policy.thresholds, name)
        scores = [row.scores[answer] for row in rows if answer in row.scores]
        under = [score for score in scores if not crossed(name, score, policy.thresholds)]
        result.append(
            {
                "name": name,
                "value": limit,
                "answer": answer,
                "direction": direction,
                "effect": effect,
                "scale_max": RISK_TOP if answer == "risk" else 1,
                "scores": scores,
                "judged": len(scores),
                "crossed": len(scores) - len(under),
                "nearest": min(under, key=lambda score: abs(score - limit), default=None),
            }
        )
    return result


class Answer(BaseModel):
    decision: Literal["approve", "deny"]


def create_app(state_dir: Path, policy_path: Path, static_dir: Path = STATIC) -> FastAPI:
    app = FastAPI(title="Tollgate", docs_url=None, redoc_url=None, openapi_url=None)
    # A page on another site can make the browser send a request to localhost, but it
    # cannot read the reply or add a custom header. An answer to a held call must
    # carry this token in a header, so only the dashboard's own pages can send one.
    token = secrets.token_urlsafe(32)

    def recent(limit: int) -> list[Row]:
        path = state_dir / AUDIT_FILE
        if not path.exists():
            return []
        with AuditStore(path) as audit:
            return [describe(record) for record in audit.recent(limit)]

    def pending() -> list[dict[str, Any]]:
        path = state_dir / APPROVALS_FILE
        if not path.exists():
            return []
        with ApprovalQueue(path) as queue:
            held = queue.pending()
        now = datetime.now(UTC)
        return [
            {
                "id": call.id,
                "tool": call.tool,
                "upstream": call.upstream,
                "reasons": list(call.reasons),
                "user_request": call.user_request,
                "arguments": preview_arguments(call.arguments),
                "created": call.created.isoformat(),
                "expires": call.expires.isoformat(),
                "seconds_left": max(int((call.expires - now).total_seconds()), 0),
            }
            for call in held
        ]

    def clamp(calls: int) -> int:
        return max(1, min(calls, MOST_CALLS))

    @app.get("/api/session")
    def session() -> dict[str, str]:
        return {"token": token, "state_dir": str(state_dir), "policy": str(policy_path)}

    @app.get("/api/decisions")
    def decisions(calls: int = DEFAULT_CALLS) -> dict[str, Any]:
        rows = recent(clamp(calls))
        totals = Counter((row.outcome, row.kind) for row in rows)
        return {
            "calls": [as_json(row) for row in rows],
            "totals": [
                {"outcome": outcome, "kind": kind, "count": count}
                for (outcome, kind), count in totals.most_common()
            ],
            "waiting": len(pending()),
        }

    @app.get("/api/approvals")
    def approvals() -> dict[str, Any]:
        return {"held": pending()}

    @app.post("/api/approvals/{call_id}")
    def answer(
        call_id: int,
        body: Answer,
        x_tollgate_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        if x_tollgate_token is None or not hmac.compare_digest(x_tollgate_token, token):
            raise HTTPException(403, "This request did not come from the Tollgate dashboard.")
        path = state_dir / APPROVALS_FILE
        if not path.exists():
            raise HTTPException(404, "There is no approval queue yet.")
        approve = body.decision == "approve"
        try:
            with ApprovalQueue(path) as queue:
                queue.answer(call_id, approve=approve)
        except ApprovalError as error:
            return {"changed": False, "note": f"Call #{call_id} was not changed: {error}"}
        return {
            "changed": True,
            "note": f"Call #{call_id} was {'approved' if approve else 'denied'}.",
        }

    @app.get("/api/thresholds")
    def thresholds(calls: int = DEFAULT_CALLS) -> dict[str, Any]:
        try:
            policy = load_policy(policy_path)
        except PolicyError as error:
            return {"policy": str(policy_path), "problem": str(error)}
        rows = [row for row in recent(clamp(calls)) if row.scores]
        return {
            "policy": str(policy_path),
            "problem": None,
            "judged": len(rows),
            "thresholds": threshold_rows(policy, rows),
            "confidence": policy.thresholds.confidence,
            "result_scan": {
                "enabled": policy.result_scan.enabled,
                "injected": policy.result_scan.injected,
            },
            "second_model": {
                "enabled": policy.escalation.enabled,
                "provider": policy.escalation.provider,
                "model": policy.escalation.model,
            },
        }

    if static_dir.is_dir():
        # After the API routes, so that it only answers what they do not.
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="pages")
    else:

        @app.get("/", response_class=PlainTextResponse)
        def not_built() -> str:
            return (
                "The dashboard's pages have not been built.\n"
                "In the repo's dashboard/ folder, run: npm install && npm run build\n"
                "The API is running; see /api/decisions."
            )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tollgate-dashboard",
        description="A local web app showing what Tollgate decided, and the calls it is holding.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".tollgate"),
        metavar="DIR",
        help="Tollgate's state folder (default: %(default)s)",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("policy.yaml"),
        metavar="FILE",
        help="the policy file the proxy runs with (default: %(default)s)",
    )
    parser.add_argument("--port", type=int, default=8787, help="default: %(default)s")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to listen on; anyone who can reach it can approve calls "
        "(default: %(default)s)",
    )
    args = parser.parse_args()
    print(f"Tollgate dashboard: http://{args.host}:{args.port}")
    uvicorn.run(
        create_app(args.state_dir, args.policy),
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
