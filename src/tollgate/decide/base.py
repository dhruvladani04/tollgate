"""The decision interface: what Tollgate asks a model about a call, and what comes back.

A request has the same shape as a System One request, a `state` plus named
questions, so any backend that can answer that shape can stand behind the gate.
"""

from collections.abc import Mapping
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class NoulAnswer(BaseModel):
    """The probability that a statement about the state is true."""

    model_config = ConfigDict(frozen=True)

    type: Literal["noul"] = "noul"
    probability: float


class ScoreAnswer(BaseModel):
    """A position on a scale of ordered levels, with the probability of each level."""

    model_config = ConfigDict(frozen=True)

    type: Literal["score"] = "score"
    score: float
    probabilities: tuple[float, ...]
    confidence: float


Answer = Annotated[NoulAnswer | ScoreAnswer, Field(discriminator="type")]


class DecisionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: Mapping[str, Any]
    questions: Mapping[str, Mapping[str, Any]]


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    answers: dict[str, Answer]
    model: str
    """The versioned model that answered, as the backend reported it."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached: bool = False


DecisionFailure = Literal["timeout", "rate_limited", "unreachable", "failed"]


class DecisionError(Exception):
    """The model gave no usable decision. `kind` says why."""

    def __init__(self, kind: DecisionFailure, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class DecisionModel(Protocol):
    backend: str
    """A short name for the backend, such as `jev`."""
    model: str
    """The pinned model this backend asks for."""

    async def decide(self, request: DecisionRequest) -> Decision: ...


# The environment variable that holds each LLM provider's API key. They are named
# here, apart from the judge itself, so that code which only needs the names stays light.
LLM_API_KEY_ENVS = {
    "gemini": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}
