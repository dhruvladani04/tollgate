"""Jev access: the pinned model version and the route used to reach it.

Thresholds are tuned against one Jev version, so the model is pinned and never
a moving alias. Each route names that version differently, so the pin lives
here, per route, and nowhere else.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import typesafe_sdk as sdk
from typesafe_sdk import AsyncTypeSafeClient

from tollgate.decide.base import (
    Answer,
    Decision,
    DecisionError,
    DecisionRequest,
    NoulAnswer,
    ScoreAnswer,
)

ROUTE_ENV = "TOLLGATE_JEV_ROUTE"
DEFAULT_ROUTE = "typesafe"


@dataclass(frozen=True)
class JevRoute:
    name: str
    base_url: str
    api_key_env: str
    model: str


ROUTES: dict[str, JevRoute] = {
    "typesafe": JevRoute(
        name="typesafe",
        base_url="https://api.typesafe.ai",
        api_key_env="TYPESAFE_API_KEY",
        model="jev-1.13.0",
    ),
    "openrouter": JevRoute(
        name="openrouter",
        base_url="https://openrouter.ai/api",
        api_key_env="OPENROUTER_API_KEY",
        model="typesafe/jev-1.13",
    ),
}


class JevConfigError(RuntimeError):
    """The environment does not describe a usable Jev route."""


def resolve_route(env: Mapping[str, str] | None = None) -> JevRoute:
    env = os.environ if env is None else env
    name = env.get(ROUTE_ENV, "").strip().lower() or DEFAULT_ROUTE
    if name not in ROUTES:
        known = ", ".join(sorted(ROUTES))
        raise JevConfigError(f"{ROUTE_ENV}={name!r} is not a known route (one of: {known})")
    return ROUTES[name]


def read_api_key(route: JevRoute, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    key = env.get(route.api_key_env, "").strip()
    if not key:
        known = ", ".join(sorted(ROUTES))
        raise JevConfigError(
            f"{route.api_key_env} is not set (route {route.name!r}). "
            f"Set it, or pick another route with {ROUTE_ENV} (one of: {known})."
        )
    return key


def make_client(
    route: JevRoute,
    *,
    env: Mapping[str, str] | None = None,
    **client_options: Any,
) -> AsyncTypeSafeClient:
    """Build an SDK client bound to the route's endpoint and pinned model.

    `client_options` go to `AsyncTypeSafeClient` unchanged (retry, timeout, transport).
    """
    return AsyncTypeSafeClient(
        api_key=read_api_key(route, env),
        base_url=route.base_url,
        model=route.model,
        **client_options,
    )


class JevDecisionModel:
    """Asks Jev, through an SDK client the caller opens and closes."""

    backend = "jev"

    def __init__(self, client: AsyncTypeSafeClient, model: str) -> None:
        self._client = client
        self.model = model

    async def decide(self, request: DecisionRequest) -> Decision:
        try:
            response = await self._client.system_one(
                dict(request.state), dict(request.questions), model=self.model
            )
        except sdk.TypeSafeAPITimeoutError as error:
            raise DecisionError("timeout", str(error)) from error
        except sdk.TypeSafeAPIConnectionError as error:
            raise DecisionError("unreachable", str(error)) from error
        except sdk.TypeSafeRateLimitError as error:
            raise DecisionError("rate_limited", str(error)) from error
        except sdk.TypeSafeError as error:
            raise DecisionError("failed", str(error)) from error

        unanswered = sorted(set(request.questions) - set(response.answers))
        if unanswered:
            raise DecisionError("failed", f"Jev did not answer: {', '.join(unanswered)}")
        return Decision(
            answers={name: _answer(response.answers[name]) for name in request.questions},
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


def _answer(answer: sdk.Answer) -> Answer:
    match answer:
        case sdk.NoulAnswer():
            return NoulAnswer(probability=answer.noul)
        case sdk.ScoreAnswer():
            levels = sorted(answer.probabilities)
            return ScoreAnswer(
                score=answer.score,
                probabilities=tuple(answer.probabilities[level] for level in levels),
                confidence=answer.confidence,
            )
        case _:
            raise DecisionError("failed", f"Jev returned an unsupported {answer.type} answer")
