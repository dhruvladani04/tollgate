"""Stand-ins shared by the proxy and decision tests."""

import asyncio
from pathlib import Path

import yaml
from mcp import types

from tollgate.decide.base import Decision, DecisionRequest, NoulAnswer, ScoreAnswer
from tollgate.policy.schema import Policy, parse_policy


def tool(name: str, description: str | None = None) -> types.Tool:
    return types.Tool(name=name, description=description, input_schema={"type": "object"})


def text_result(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


def call(name: str, **arguments) -> types.CallToolRequestParams:
    return types.CallToolRequestParams(name=name, arguments=arguments)


class FakeUpstream:
    def __init__(self, *tool_names: str) -> None:
        self.tool_names = list(tool_names)
        self.calls: list[types.CallToolRequestParams] = []
        self.listings = 0

    async def list_tools(self) -> list[types.Tool]:
        self.listings += 1
        return [tool(name, f"The {name} tool.") for name in self.tool_names]

    async def call_tool(self, params: types.CallToolRequestParams) -> types.CallToolResult:
        self.calls.append(params)
        return text_result(f"{params.name} ran")


DECISION = Decision(
    answers={
        "risk": ScoreAnswer(score=2.29, probabilities=(0.01, 0.01, 0.67, 0.31), confidence=0.66),
        "destructive": NoulAnswer(probability=0.87),
    },
    model="jev-1.13.0",
    input_tokens=431,
    output_tokens=58,
)


class FakeDecisionModel:
    """Returns one fixed decision, or raises one fixed error, and records what it was asked."""

    backend = "fake"
    model = "fake-1"

    def __init__(self, outcome: Decision | Exception = DECISION, delay: float = 0.0) -> None:
        self.outcome = outcome
        self.delay = delay
        self.requests: list[DecisionRequest] = []

    async def decide(self, request: DecisionRequest) -> Decision:
        self.requests.append(request)
        await asyncio.sleep(self.delay)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


EXAMPLE_POLICY = Path(__file__).resolve().parents[1] / "policy.example.yaml"


def policy(**sections: dict) -> Policy:
    """The example policy with its exact checks switched off, then any changes applied.

    Each keyword names a section and gives the fields to change in it, for example
    `policy(approval={"timeout_seconds": 1})`.
    """
    document = yaml.safe_load(EXAMPLE_POLICY.read_text(encoding="utf-8"))
    document["prefilter"]["allow_tools"] = []
    document["prefilter"]["path_sandbox"]["roots"] = []
    for section, changes in sections.items():
        if section == "tools":
            document["tools"] = changes
        else:
            document[section] |= changes
    return parse_policy(document)


def decision(
    risk: float = 0.0,
    destructive: float = 0.02,
    on_task: float | None = None,
    exfiltration: float | None = None,
) -> Decision:
    """A decision with clear answers, harmless unless a value says otherwise."""
    answers: dict = {
        "risk": ScoreAnswer(score=risk, probabilities=(1.0, 0.0, 0.0, 0.0), confidence=1.0),
        "destructive": NoulAnswer(probability=destructive),
    }
    if on_task is not None:
        answers["on_task"] = NoulAnswer(probability=on_task)
    if exfiltration is not None:
        answers["exfiltration"] = NoulAnswer(probability=exfiltration)
    return Decision(answers=answers, model="jev-1.13.0", input_tokens=431, output_tokens=58)
