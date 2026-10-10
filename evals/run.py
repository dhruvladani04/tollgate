"""Ask Jev about every evaluation case and store the answers.

Run with `uv run --env-file .env python -m evals.run`. It needs an API key.

Every case is asked twice: once with the user's request and once without it,
because a client such as Claude Code may or may not supply one. Answers go into
a cache keyed on the exact request, so a second run asks nothing and costs
nothing, and so the report can be rebuilt by someone who has no key.

A case whose arguments hold a secret is never sent, exactly as in the gate.

It then measures stability: a sample of test cases is asked five more times,
one call at a time, with the timing of each call recorded.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import date

from typesafe_sdk import RetryPolicy, TypeSafeError

from evals.data import (
    CACHE,
    META,
    MODEL,
    REPEATS,
    Case,
    all_cases,
    carries_secret,
    key_for,
    request_for,
    start_policy,
)
from tollgate.decide.base import DecisionError
from tollgate.decide.cache import DecisionCache
from tollgate.decide.jev import JevConfigError, JevDecisionModel, make_client, resolve_route

# How many requests are in flight at once while filling the cache.
CONCURRENCY = 6
# Every nth golden test case is used for the stability and timing sample.
REPEAT_EVERY = 4
REPEAT_COUNT = 5


async def fill_cache(model: JevDecisionModel, cases: list[Case], cache: DecisionCache) -> int:
    """Ask the model for every request not yet in the cache; return how many were asked."""
    policy = start_policy()
    pending = {}
    for case in cases:
        if carries_secret(case, policy):
            continue
        for with_request in (True, False):
            key = key_for(case, with_request)
            if cache.get(key) is None:
                pending[key] = request_for(case, with_request)

    limit = asyncio.Semaphore(CONCURRENCY)
    done = 0

    async def ask(key, request) -> None:
        nonlocal done
        async with limit:
            decision = await model.decide(request)
        cache.put(key, decision)
        done += 1
        if done % 100 == 0:
            print(f"  {done} of {len(pending)} answered", flush=True)

    await asyncio.gather(*(ask(key, request) for key, request in pending.items()))
    return len(pending)


async def measure_repeats(model: JevDecisionModel, cases: list[Case]) -> int:
    """Ask a sample of test cases several more times, one at a time, timing each call."""
    policy = start_policy()
    sample = [
        case
        for case in cases
        if case["source"] == "golden"
        and case["split"] == "test"
        and not carries_secret(case, policy)
    ][::REPEAT_EVERY]
    recorded = set()
    if REPEATS.exists():
        for line in REPEATS.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            recorded.add((entry["case"], entry["repeat"]))
    asked = 0
    REPEATS.parent.mkdir(parents=True, exist_ok=True)
    with REPEATS.open("a", encoding="utf-8", newline="\n") as file:
        for case in sample:
            request = request_for(case, with_user_request=True)
            for repeat in range(REPEAT_COUNT):
                if (case["id"], repeat) in recorded:
                    continue
                started = time.perf_counter()
                decision = await model.decide(request)
                latency_ms = (time.perf_counter() - started) * 1000
                entry = {
                    "case": case["id"],
                    "repeat": repeat,
                    "latency_ms": round(latency_ms, 1),
                    "decision": decision.model_dump(mode="json"),
                }
                file.write(json.dumps(entry) + "\n")
                file.flush()
                asked += 1
    return asked


async def run(skip_repeats: bool) -> None:
    route = resolve_route()
    cases = all_cases()
    cache = DecisionCache(CACHE)
    before = len(cache)
    async with make_client(route, retry=RetryPolicy(max_retries=4)) as client:
        model = JevDecisionModel(client, route.model)
        print(f"asking {route.model} over the {route.name} route; cache key model is {MODEL}")
        asked = await fill_cache(model, cases, cache)
        print(f"cache: {before} answers before, {asked} asked now, {len(cache)} in total")
        if asked:
            # Recorded so the report can say when and by which model the answers were given.
            META.write_text(
                json.dumps(
                    {"asked_on": date.today().isoformat(), "model": MODEL, "route": route.name}
                ),
                encoding="utf-8",
            )
        if not skip_repeats:
            repeated = await measure_repeats(model, cases)
            print(f"stability sample: {repeated} timed calls made")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-repeats", action="store_true", help="only fill the answer cache")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.skip_repeats))
    except (JevConfigError, TypeSafeError, DecisionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
