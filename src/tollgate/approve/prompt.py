"""What a person is shown when Tollgate asks them about a held call."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

# The name of Tollgate's question when it asks through the MCP client.
APPROVAL_KEY = "tollgate_approval"
# A form with no fields: the client shows the message with accept and decline.
APPROVAL_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
# How much of a call's arguments a person is shown before they are cut off.
ARGUMENT_PREVIEW_CHARS = 300


def preview_arguments(arguments: Mapping[str, Any] | None) -> str:
    text = json.dumps(arguments or {}, ensure_ascii=False)
    if len(text) > ARGUMENT_PREVIEW_CHARS:
        text = text[:ARGUMENT_PREVIEW_CHARS] + " ..."
    return text


def approval_message(
    *,
    call_id: int,
    tool: str,
    upstream: str,
    reasons: Sequence[str],
    arguments: Mapping[str, Any] | None,
) -> str:
    """The question put to the user in the MCP client."""
    why = "\n".join(f"- {reason}" for reason in reasons)
    return (
        f"Tollgate is holding a tool call (held call #{call_id}).\n\n"
        f"Tool: {tool} (from {upstream})\n"
        f"Arguments: {preview_arguments(arguments)}\n\n"
        f"Why it was held:\n{why}\n\n"
        "Accept to let the call run. Decline to refuse it."
    )
