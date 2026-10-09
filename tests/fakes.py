"""Stand-ins shared by the proxy and decision tests."""

from mcp import types

from tollgate.decide.base import Decision, DecisionRequest, NoulAnswer, ScoreAnswer


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

    def __init__(self, outcome: Decision | Exception = DECISION) -> None:
        self.outcome = outcome
        self.requests: list[DecisionRequest] = []

    async def decide(self, request: DecisionRequest) -> Decision:
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome
