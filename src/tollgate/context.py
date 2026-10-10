"""What Tollgate knows about the conversation around a tool call.

This module imports nothing heavy, because the Claude Code hook loads it on
every prompt.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# How a client tells Tollgate what the user asked for. MCP carries no such field,
# so a client that knows it sends it in the `_meta` of each tool call.
USER_REQUEST_META = "io.github.dhruvladani04.tollgate/user_request"
RECENT_CONTEXT_META = "io.github.dhruvladani04.tollgate/recent_context"

# The spec limits `recent_context` to the last two turns.
RECENT_TURNS = 2


@dataclass(frozen=True)
class CallContext:
    """What the client said about the conversation around a call."""

    user_request: str | None = None
    recent_context: tuple[str, ...] = ()


def context_from_meta(meta: Mapping[str, Any] | None) -> CallContext:
    """Read the call context from a tool call's `_meta`, ignoring anything malformed."""
    meta = meta or {}
    user_request = meta.get(USER_REQUEST_META)
    turns = meta.get(RECENT_CONTEXT_META)
    return CallContext(
        user_request=user_request if isinstance(user_request, str) and user_request else None,
        recent_context=(
            tuple(turn for turn in turns if isinstance(turn, str))
            if isinstance(turns, list)
            else ()
        ),
    )


def trim(text: str, max_chars: int) -> str:
    """Cut the middle out of a long text, keeping its start and its end.

    A long prompt often opens with the request or closes with it, after pasted
    material, so both ends are kept.
    """
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2
    left_out = len(text) - 2 * keep
    return f"{text[:keep]} [... {left_out} characters left out ...] {text[len(text) - keep :]}"


def trimmed(context: CallContext, max_chars: int) -> CallContext:
    """The context with the request and every turn cut to `max_chars`."""
    return CallContext(
        user_request=(
            None if context.user_request is None else trim(context.user_request, max_chars)
        ),
        recent_context=tuple(trim(turn, max_chars) for turn in context.recent_context),
    )
