"""Jev access: the pinned model version and the route used to reach it.

Thresholds are tuned against one Jev version, so the model is pinned and never
a moving alias. Each route names that version differently, so the pin lives
here, per route, and nowhere else.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from typesafe_sdk import AsyncTypeSafeClient

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
