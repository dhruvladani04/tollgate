import asyncio
import json

import httpx2
import smoke_jev

from tollgate.decide.jev import ROUTES, make_client

# Shaped like the response body in the TypeSafe API reference.
RESPONSE = {
    "model": "jev-1.13.0",
    "answers": {
        "destructive": {"type": "noul", "noul": 0.97},
        "risk": {
            "type": "score",
            "score": 2.05,
            "confidence": 0.8,
            "legend": {"0": "level 0", "1": "level 1", "2": "level 2", "3": "level 3"},
            "probabilities": {"0": 0.0, "1": 0.05, "2": 0.85, "3": 0.1},
        },
        "tool_family": {
            "type": "choice",
            "choice": "git",
            "confidence": 0.88,
            "probabilities": {
                "filesystem": 0.05,
                "git": 0.9,
                "http": 0.05,
                "database": 0.0,
                "email": 0.0,
                "other": 0.0,
            },
        },
    },
    "usage": {"input_tokens": 431, "output_tokens": 58},
}


def run_against_fake_api(route_name="typesafe"):
    """Run the smoke request against a stub endpoint; return (requests seen, response)."""
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=RESPONSE)

    client = make_client(
        ROUTES[route_name],
        env={ROUTES[route_name].api_key_env: "test-key"},
        transport=httpx2.MockTransport(handler),
    )
    response, _ = asyncio.run(smoke_jev.ask(client))
    return seen, response


def test_request_goes_to_the_systemone_endpoint_with_the_pinned_model():
    seen, _ = run_against_fake_api()

    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.typesafe.ai/v1/systemone"
    assert json.loads(seen[0].content)["model"] == "jev-1.13.0"


def test_openrouter_route_sends_its_pinned_id_to_openrouter():
    seen, _ = run_against_fake_api("openrouter")

    assert str(seen[0].url) == "https://openrouter.ai/api/v1/systemone"
    assert json.loads(seen[0].content)["model"] == "typesafe/jev-1.13"


def test_request_asks_one_noul_one_score_and_one_choice():
    seen, _ = run_against_fake_api()
    questions = json.loads(seen[0].content)["questions"]

    assert sorted(question["type"] for question in questions.values()) == [
        "choice",
        "noul",
        "score",
    ]


def test_state_holds_only_the_request_and_the_proposed_call():
    seen, _ = run_against_fake_api()

    assert set(json.loads(seen[0].content)["state"]) == {"user_request", "proposed_call"}


def test_render_prints_each_typed_answer_and_the_answering_model():
    _, response = run_against_fake_api()

    output = smoke_jev.render(ROUTES["typesafe"], response, elapsed_ms=412.4)

    assert "answered by  jev-1.13.0" in output
    assert "round trip   412 ms" in output
    assert "input=431 output=58" in output
    assert "destructive  noul    p(yes)=0.970" in output
    assert "risk         score   2.050 on 0-3  confidence=0.800" in output
    assert "tool_family  choice  git  confidence=0.880" in output
