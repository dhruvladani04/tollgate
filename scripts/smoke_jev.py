"""M0 smoke test: one Jev request with a Noul, a Score and a Choice.

Run with `uv run python scripts/smoke_jev.py`. The API key for the selected
route must be in the environment; see `tollgate.decide.jev` for the routes.
"""

import asyncio
import sys
import time

from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    SystemOneResponse,
    TypeSafeError,
)

from tollgate.decide.jev import JevConfigError, JevRoute, make_client, resolve_route

STATE = {
    "user_request": "Clean up the feature branches that are already merged.",
    "proposed_call": {
        "tool": "git_push",
        "description": "Push local refs to a remote repository.",
        "arguments": {"remote": "origin", "refspec": "main", "force": True},
    },
}

QUESTIONS = {
    "destructive": Noul(
        instructions="`proposed_call` deletes, overwrites or force-pushes data",
    ),
    "risk": Score(
        instructions="How much damage could `proposed_call` cause if it is wrong?",
        criteria=[
            "Read-only: it changes nothing",
            "A reversible change inside the workspace",
            "A change that is hard to undo or that reaches outside the workspace",
            "It destroys data, spends money or sends data to a third party",
        ],
    ),
    "tool_family": Choice(
        instructions="Which kind of system does `proposed_call` act on?",
        criteria={
            "filesystem": "Local files and directories",
            "git": "A git repository or its remotes",
            "http": "A web page or HTTP API",
            "database": "A database",
            "email": "An email account",
            "other": "None of the other options",
        },
    ),
}


async def ask(client: AsyncTypeSafeClient) -> tuple[SystemOneResponse, float]:
    """Send the smoke request; return the response and the round trip in milliseconds."""
    async with client:
        started = time.perf_counter()
        response = await client.system_one(STATE, QUESTIONS)
        return response, (time.perf_counter() - started) * 1000


def render(route: JevRoute, response: SystemOneResponse, elapsed_ms: float) -> str:
    usage = response.usage
    lines = [
        f"route        {route.name} ({route.base_url})",
        f"requested    {route.model}",
        f"answered by  {response.model}",
        f"round trip   {elapsed_ms:.0f} ms, including connection setup",
        f"tokens       input={usage.input_tokens} output={usage.output_tokens}",
        "",
    ]
    for name, answer in response.answers.items():
        match answer:
            case NoulAnswer():
                lines.append(f"{name:<12} noul    p(yes)={answer.noul:.3f}")
            case ScoreAnswer():
                top = max(answer.legend)
                lines.append(
                    f"{name:<12} score   {answer.score:.3f} on 0-{top}"
                    f"  confidence={answer.confidence:.3f}"
                )
                for level, probability in sorted(answer.probabilities.items()):
                    lines.append(f"{'':<12}   {level}  {probability:.3f}  {answer.legend[level]}")
            case ChoiceAnswer():
                lines.append(
                    f"{name:<12} choice  {answer.choice}  confidence={answer.confidence:.3f}"
                )
                ranked = sorted(answer.probabilities.items(), key=lambda item: -item[1])
                for option, probability in ranked:
                    lines.append(f"{'':<12}   {probability:.3f}  {option}")
    return "\n".join(lines)


def main() -> int:
    try:
        route = resolve_route()
        client = make_client(route)
    except (JevConfigError, TypeSafeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        response, elapsed_ms = asyncio.run(ask(client))
    except TypeSafeError as error:
        print(f"error: Jev request failed: {error}", file=sys.stderr)
        return 1
    print(render(route, response, elapsed_ms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
