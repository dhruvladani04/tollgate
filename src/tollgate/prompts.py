"""The user's recent prompts, handed to Tollgate by a Claude Code hook.

MCP tells a server nothing about the conversation. Claude Code can run a command
on every prompt, though, and `tollgate-hook` is that command. It records the
prompt in a small file in Tollgate's state folder, and the gate reads that file
when it judges a call.
"""

import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tollgate.context import RECENT_TURNS, CallContext

PROMPTS_FILE = "prompts.json"
# The latest prompt is the request; the ones before it are its recent context.
KEPT_PROMPTS = RECENT_TURNS + 1
# Replacing the file can fail for an instant on Windows while a reader has it open.
_WRITE_ATTEMPTS = 5
_WRITE_RETRY_SECONDS = 0.05


@dataclass(frozen=True)
class Prompt:
    at: datetime
    text: str


class PromptLog:
    """The last few prompts of the current Claude Code session, kept in one file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, session_id: str, text: str, at: datetime | None = None) -> None:
        """Add a prompt. A prompt from a new session starts the list again."""
        stored_session, prompts = self._read()
        if stored_session != session_id:
            prompts = []
        prompts = [*prompts, Prompt(at or datetime.now(UTC), text)][-KEPT_PROMPTS:]
        document = {
            "session_id": session_id,
            "prompts": [{"at": prompt.at.isoformat(), "text": prompt.text} for prompt in prompts],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        for attempt in range(_WRITE_ATTEMPTS):
            try:
                os.replace(temporary, self._path)
                return
            except PermissionError:
                if attempt == _WRITE_ATTEMPTS - 1:
                    temporary.unlink(missing_ok=True)
                    raise
                time.sleep(_WRITE_RETRY_SECONDS)

    def context(self, since: datetime) -> CallContext | None:
        """The call context from prompts submitted after `since`, if there are any.

        A prompt from before this Tollgate process started belongs to an earlier
        session, so it says nothing about the calls being made now.
        """
        _, prompts = self._read()
        fresh = [prompt for prompt in prompts if prompt.at >= since]
        if not fresh:
            return None
        return CallContext(
            user_request=fresh[-1].text,
            recent_context=tuple(prompt.text for prompt in fresh[:-1]),
        )

    def _read(self) -> tuple[str | None, list[Prompt]]:
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
            prompts = [
                Prompt(datetime.fromisoformat(entry["at"]), entry["text"])
                for entry in document["prompts"]
            ]
            return document.get("session_id"), prompts
        except (OSError, ValueError, KeyError, TypeError):
            # A missing or unreadable file means there is no context to give.
            return None, []


def record_hook_event(arguments: Sequence[str], payload: bytes, environ: Mapping[str, str]) -> None:
    """Handle one hook invocation: record the prompt if the event carries one."""
    state_dir = Path(".tollgate")
    if arguments:
        if len(arguments) != 2 or arguments[0] != "--state-dir":
            raise ValueError("usage: tollgate-hook [--state-dir DIR]")
        state_dir = Path(arguments[1])

    event = json.loads(payload.decode("utf-8", errors="replace"))
    if event.get("hook_event_name") != "UserPromptSubmit":
        return
    if event.get("agent_id"):
        # A prompt inside a subagent is not the user's request.
        return
    prompt = event.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return
    project = Path(environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".")
    PromptLog(project / state_dir / PROMPTS_FILE).record(str(event.get("session_id") or ""), prompt)


def main() -> int:
    """The `tollgate-hook` command, run by Claude Code on every prompt.

    It prints nothing on stdout, because Claude Code would show that to the
    model. It never exits with status 2, because that would block the prompt.
    """
    try:
        record_hook_event(sys.argv[1:], sys.stdin.buffer.read(), os.environ)
    except Exception as error:
        print(f"tollgate-hook: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
