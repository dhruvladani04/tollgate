"""Shared pieces of the evaluation: the cases, the request for each, and the policy under test."""

import json
from pathlib import Path
from typing import Any

import yaml
from mcp import types

from tollgate.context import CallContext
from tollgate.decide.base import DecisionRequest
from tollgate.decide.cache import request_key
from tollgate.decide.questions import call_request
from tollgate.policy.engine import Verdict
from tollgate.policy.prefilter import prefilter
from tollgate.policy.schema import Policy, Thresholds, parse_policy

EVALS = Path(__file__).parent
REPO = EVALS.parent
GOLDEN_CALLS = EVALS / "data" / "golden_calls.jsonl"
INJECAGENT_CALLS = EVALS / "data" / "external" / "injecagent_calls.jsonl"
CACHE = EVALS / "cache" / "jev-1.13.0.jsonl"
REPEATS = EVALS / "cache" / "repeats.jsonl"
META = EVALS / "cache" / "meta.json"

# Cache keys always use the direct route's model id, so a cache filled through
# one route is found through another: both reach the same pinned model.
MODEL = "jev-1.13.0"
BACKEND = "jev"

# The golden filesystem scenes treat this folder as the workspace.
WORKSPACE = "/workspace/shop-api"
# Path checks resolve relative paths against this folder.
CWD = Path("/")

Case = dict[str, Any]


def load_cases(path: Path) -> list[Case]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def all_cases() -> list[Case]:
    return load_cases(GOLDEN_CALLS) + load_cases(INJECAGENT_CALLS)


def request_for(case: Case, with_user_request: bool) -> DecisionRequest:
    """The exact request the gate would send to the model for this case."""
    tool = types.Tool(
        name=case["tool"]["name"],
        description=case["tool"]["description"],
        input_schema={"type": "object"},
    )
    context = (
        CallContext(user_request=case["user_request"], recent_context=tuple(case["recent_context"]))
        if with_user_request
        else CallContext()
    )
    return call_request(tool, case["arguments"], context)


def key_for(case: Case, with_user_request: bool) -> str:
    return request_key(BACKEND, MODEL, request_for(case, with_user_request))


def start_policy() -> Policy:
    """The shipped example policy, pointed at the golden set's workspace."""
    document = yaml.safe_load((REPO / "policy.example.yaml").read_text(encoding="utf-8"))
    document["prefilter"]["path_sandbox"]["roots"] = [WORKSPACE]
    document["prefilter"]["allow_tools"] = []
    return parse_policy(document)


def with_thresholds(policy: Policy, thresholds: Thresholds) -> Policy:
    return policy.model_copy(update={"thresholds": thresholds})


def exact_verdict(case: Case, policy: Policy) -> Verdict | None:
    """What the pre-filter decides about this case, or None when it leaves it to the model."""
    return prefilter(case["tool"]["name"], case["arguments"], policy.prefilter, CWD)


def carries_secret(case: Case, policy: Policy) -> bool:
    """True when the pre-filter finds a secret, so the case must never be sent to the model."""
    verdict = exact_verdict(case, policy)
    return verdict is not None and "secret" in verdict.reasons[0]
