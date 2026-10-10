"""Measure how long Jev takes to answer the gate's questions from this machine.

Run with `uv run --env-file .env python scripts/measure_jev_latency.py`.

It sends distinct requests one after another over a single connection, with the
cache and retries off, so every sample is one real round trip. The first call
is reported on its own because it also pays for connection setup.
"""

import argparse
import asyncio
import statistics
import sys
import time

from mcp import types
from typesafe_sdk import RetryPolicy, TypeSafeError

from tollgate.context import CallContext
from tollgate.decide.base import Decision, DecisionError
from tollgate.decide.jev import JevConfigError, JevDecisionModel, make_client, resolve_route
from tollgate.decide.questions import call_request

TOOL = types.Tool(
    name="write_file",
    description=(
        "Create a new file or completely overwrite an existing file with new content. "
        "Only works within allowed directories."
    ),
    input_schema={"type": "object"},
)
CONTEXT = CallContext(user_request="Save a short summary of today's meeting in the notes folder.")


async def measure(calls: int) -> tuple[list[float], list[Decision], list[str]]:
    """Time `calls` distinct requests plus one warm-up; return times in ms, decisions, errors."""
    route = resolve_route()
    timings: list[float] = []
    decisions: list[Decision] = []
    errors: list[str] = []
    async with make_client(route, retry=RetryPolicy(max_retries=0)) as client:
        model = JevDecisionModel(client, route.model)
        for index in range(calls + 1):
            arguments = {"path": f"notes/summary-{index}.md", "content": f"Summary number {index}."}
            request = call_request(TOOL, arguments, CONTEXT)
            started = time.perf_counter()
            try:
                decisions.append(await model.decide(request))
            except DecisionError as error:
                errors.append(f"{error.kind}: {error}")
                continue
            timings.append((time.perf_counter() - started) * 1000)
    return timings, decisions, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--calls", type=int, default=30, help="warm calls to time (default: 30)")
    args = parser.parse_args()

    try:
        route = resolve_route()
        timings, decisions, errors = asyncio.run(measure(args.calls))
    except (JevConfigError, TypeSafeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if len(timings) < 3:
        print(f"error: too few calls succeeded to report. Errors: {errors}", file=sys.stderr)
        return 1

    cold, warm = timings[0], sorted(timings[1:])
    tokens = [decision.input_tokens or 0 for decision in decisions]
    print(f"route            {route.name} ({route.base_url})")
    print(f"answered by      {decisions[0].model}")
    print(f"questions        {len(decisions[0].answers)} per request")
    print(f"input tokens     {statistics.mean(tokens):.0f} per request on average")
    print(f"first call       {cold:.0f} ms, including connection setup")
    print(f"warm calls       {len(warm)} timed, {len(errors)} failed")
    print(f"  median (p50)   {statistics.median(warm):.0f} ms")
    print(f"  p95            {statistics.quantiles(warm, n=20)[-1]:.0f} ms")
    print(f"  fastest        {warm[0]:.0f} ms")
    print(f"  slowest        {warm[-1]:.0f} ms")
    for error in errors:
        print(f"  failed: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
