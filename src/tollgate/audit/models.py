"""What Tollgate records about each tool call."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from tollgate.decide.base import Answer


class AuditRecord(BaseModel):
    """One tool call: what was asked, what the model answered and what Tollgate did."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    mode: str
    action: str
    """What happened to the call, such as `forwarded`."""
    upstream: str
    tool: str
    arguments: dict[str, Any] | None = None
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
    answers: dict[str, Answer] = {}
    error: str | None = None
    """Why there is no decision, when the model gave none."""
