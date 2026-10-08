from pathlib import Path

import pytest

from tollgate.decide.jev import (
    ROUTES,
    JevConfigError,
    make_client,
    read_api_key,
    resolve_route,
)

REPO = Path(__file__).resolve().parents[1]
CODE_DIRS = ("src", "scripts", "tests", "evals", "examples")
CODE_SUFFIXES = {".py", ".toml", ".yaml", ".yml", ".json"}
# Built by concatenation so this file does not trip its own scan.
MOVING_ALIASES = ("jev-" + "latest", "jev-" + "preview")


def test_default_route_is_typesafe_pinned_to_1_13_0():
    route = resolve_route({})

    assert route.name == "typesafe"
    assert route.model == "jev-1.13.0"
    assert route.api_key_env == "TYPESAFE_API_KEY"


def test_openrouter_route_uses_its_own_pinned_id_and_key():
    route = resolve_route({"TOLLGATE_JEV_ROUTE": " OpenRouter "})

    assert route.name == "openrouter"
    assert route.model == "typesafe/jev-1.13"
    assert route.api_key_env == "OPENROUTER_API_KEY"
    assert route.base_url == "https://openrouter.ai/api"


def test_unknown_route_is_rejected_and_lists_the_known_ones():
    with pytest.raises(JevConfigError, match="openrouter, typesafe"):
        resolve_route({"TOLLGATE_JEV_ROUTE": "vercel"})


@pytest.mark.parametrize("route", ROUTES.values(), ids=lambda route: route.name)
def test_no_route_pins_a_moving_alias(route):
    assert "latest" not in route.model
    assert "preview" not in route.model
    assert not route.model.startswith("~")


@pytest.mark.parametrize("env", [{}, {"TYPESAFE_API_KEY": "   "}], ids=["unset", "blank"])
def test_missing_key_names_the_variable(env):
    with pytest.raises(JevConfigError, match="TYPESAFE_API_KEY is not set"):
        read_api_key(ROUTES["typesafe"], env)


def test_key_is_read_from_the_route_variable_only():
    env = {"TYPESAFE_API_KEY": "direct-key", "OPENROUTER_API_KEY": "router-key"}

    assert read_api_key(ROUTES["openrouter"], env) == "router-key"


def test_make_client_fails_before_any_request_without_a_key():
    with pytest.raises(JevConfigError):
        make_client(ROUTES["typesafe"], env={})


def test_no_moving_alias_in_code():
    offenders = [
        str(path.relative_to(REPO))
        for directory in CODE_DIRS
        for path in (REPO / directory).rglob("*")
        if path.is_file()
        and path.suffix in CODE_SUFFIXES
        and any(alias in path.read_text(encoding="utf-8") for alias in MOVING_ALIASES)
    ]

    assert offenders == []
