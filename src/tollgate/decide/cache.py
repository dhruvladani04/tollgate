"""A cache of decisions keyed on the exact request, so a repeat costs nothing.

Entries are JSON lines in one file, which keeps a cache readable and lets an
evaluation commit it so that others can reproduce its numbers without an API key.
"""

import hashlib
import json
import logging
from pathlib import Path

from pydantic import ValidationError

from tollgate.decide.base import Decision, DecisionModel, DecisionRequest

logger = logging.getLogger("tollgate")


def request_key(backend: str, model: str, request: DecisionRequest) -> str:
    """A stable hash of everything that determines the answer."""
    canonical = json.dumps(
        {
            "backend": backend,
            "model": model,
            "state": request.state,
            "questions": request.questions,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=dict,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DecisionCache:
    """Decisions stored one per line in a file, and held in memory while open."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, Decision] = {}
        if path.exists():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                try:
                    entry = json.loads(line)
                    self._entries[entry["key"]] = Decision.model_validate(entry["decision"])
                except (ValueError, KeyError, TypeError, ValidationError):
                    # A line cut short by a crash must not make the whole cache unusable.
                    logger.warning("skipping unreadable cache line %d in %s", number, path)

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: str) -> Decision | None:
        return self._entries.get(key)

    def put(self, key: str, decision: Decision) -> None:
        stored = decision.model_copy(update={"cached": False})
        self._entries[key] = stored
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"key": key, "decision": stored.model_dump(mode="json")})
        with self._path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(line + "\n")


class CachedDecisionModel:
    """Answers a repeated request from the cache and asks the backend only for new ones."""

    def __init__(self, inner: DecisionModel, cache: DecisionCache) -> None:
        self._inner = inner
        self._cache = cache
        self.backend = inner.backend
        self.model = inner.model

    async def decide(self, request: DecisionRequest) -> Decision:
        key = request_key(self.backend, self.model, request)
        hit = self._cache.get(key)
        if hit is not None:
            return hit.model_copy(update={"cached": True})
        decision = await self._inner.decide(request)
        self._cache.put(key, decision)
        return decision
