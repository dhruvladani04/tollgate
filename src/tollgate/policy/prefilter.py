"""The pre-filter: checks that code can decide exactly, run before any model is asked.

A call that these checks decide never reaches the model. That keeps exact
questions out of a probabilistic judge, and keeps secrets from being sent to it.
"""

import json
import os
import re
from collections.abc import Iterator, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from tollgate.policy.engine import Band, Verdict
from tollgate.policy.schema import PathSandbox, Prefilter


def prefilter(
    tool: str, arguments: Mapping[str, Any] | None, rules: Prefilter, cwd: Path
) -> Verdict | None:
    """Decide a call exactly, or return None to leave it to the model.

    Checks run from most to least severe, so a denied tool or a secret is
    blocked even when the tool is also on the allow list.
    """
    arguments = arguments or {}
    if tool in rules.deny_tools:
        return _verdict(Band.BLOCK, f"tool `{tool}` is on the deny list")
    secrets = sorted(
        name
        for name, pattern in rules.secret_patterns.items()
        if any(_compiled(pattern).search(text) for text in _strings(arguments))
    )
    if secrets:
        return _verdict(
            Band.BLOCK, f"the arguments contain what looks like a secret ({', '.join(secrets)})"
        )
    outside = _outside_sandbox(arguments, rules.path_sandbox, cwd)
    if outside is not None:
        return _verdict(Band.BLOCK, f"path `{outside}` is outside the allowed folders")
    if tool in rules.allow_tools:
        return _verdict(Band.ALLOW, f"tool `{tool}` is on the allow list")
    size = len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
    if size > rules.max_argument_bytes:
        return _verdict(
            Band.ASK,
            f"the arguments are {size} bytes, over the {rules.max_argument_bytes}-byte limit "
            "for what is sent to the model",
        )
    return None


def redact(arguments: Mapping[str, Any] | None, rules: Prefilter) -> dict[str, Any] | None:
    """A copy of the arguments with every secret match replaced, for logs and prompts."""
    if arguments is None:
        return None

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            for name, pattern in rules.secret_patterns.items():
                value = _compiled(pattern).sub(f"[redacted: {name}]", value)
            return value
        if isinstance(value, Mapping):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(arguments)


def _verdict(band: Band, reason: str) -> Verdict:
    return Verdict(band, (reason,), "prefilter")


@lru_cache
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _outside_sandbox(arguments: Mapping[str, Any], sandbox: PathSandbox, cwd: Path) -> str | None:
    """The first path argument that leaves every allowed folder, if there is one."""
    if not sandbox.roots:
        return None
    roots = [_normal(cwd / root) for root in sandbox.roots]
    for name in sandbox.arguments:
        value = arguments.get(name)
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            path = _normal(cwd / candidate)
            if not any(path == root or root in path.parents for root in roots):
                return candidate
    return None


def _normal(path: Path) -> Path:
    """An absolute path with `..` and symlinks resolved, and case folded where it is ignored."""
    return Path(os.path.normcase(path.resolve()))
