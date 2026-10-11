"""What Tollgate records about each tool call."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tollgate.decide.base import Answer


class AuditRecord(BaseModel):
    """One tool call: what was asked, what the model answered and what Tollgate did."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    mode: str
    band: str | None = None
    """The band the policy put the call in: allow, ask, block or escalate."""
    reasons: list[str] = Field(default_factory=list)
    """Why the call landed in that band."""
    action: str
    """What happened to the call: forwarded, blocked, denied or expired."""
    approval_id: int | None = None
    """The held call's number in the approval queue, when a person was asked."""
    upstream: str
    tool: str
    arguments: dict[str, Any] | None = None
    """The call's arguments, with anything that matched a secret pattern redacted."""
    user_request: str | None = None
    request_key: str | None = None
    """Hash of the exact decision request; the same key finds it in the cache."""
    backend: str | None = None
    model: str | None = None
    """The versioned model that answered."""
    cached: bool = False
    latency_ms: float | None = None
    """Time spent getting the decision, which is the delay Tollgate added to the call."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    answers: dict[str, Answer] = Field(default_factory=dict)
    error: str | None = None
    """Why there is no decision, when the model gave none."""
    escalation_model: str | None = None
    """The second model, when the first left the call uncertain and it was asked."""
    escalation_answers: dict[str, Answer] = Field(default_factory=dict)
    escalation_latency_ms: float | None = None
    escalation_error: str | None = None
    """Why the second model gave no answer, when it gave none."""
    result_action: str | None = None
    """What happened to the tool's result: passed, withheld or unscanned."""
    injected: float | None = None
    """The probability that the result carries instructions aimed at the agent."""
    scan_latency_ms: float | None = None
    withheld_result: str | None = None
    """The text of a result that was withheld from the agent, as it was scanned."""
