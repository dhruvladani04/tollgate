"""Ask an LLM judge about every evaluation case and store the answers.

Run with `uv run --env-file .env python -m evals.judge gemini-3.8-flash`. It
needs `GEMINI_API_KEY`, and the paid judge costs money: the run prints the
tokens it used.

The judge gets the same request Jev got, with the user's request. Answers go
into a cache keyed on the exact request, one file per judge, so a second run
asks only what is missing and the report can be rebuilt with no key. The time
each call took goes into a second file, because a cached answer has none.

A case whose arguments hold a secret is never sent, exactly as in the gate. A
case the judge fails on, after its corrective retries, is listed in a third
file and counted by the report as "no answer".
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from typesafe_sdk import RetryPolicy

from evals.data import EVALS, Case, all_cases, carries_secret, request_for, start_policy
from tollgate.decide.base import DecisionError
from tollgate.decide.cache import DecisionCache, request_key
from tollgate.decide.llm import Judge, LlmConfigError, LlmDecisionModel, resolve_judge

# Requests in flight at once, and the shortest gap between starting two of them.
# The free judge is limited to 30 requests and 16,000 tokens a minute.
PACING = {"gemini-3.8-flash": (4, 0.0), "gemma-4-31b": (2, 3.5)}


def cache_path(judge_key: str) -> Path:
    return EVALS / "cache" / f"judge-{judge_key}.jsonl"


def timings_path(judge_key: str) -> Path:
    return EVALS / "cache" / f"judge-{judge_key}-timings.jsonl"


def failures_path(judge_key: str) -> Path:
    return EVALS / "cache" / f"judge-{judge_key}-failures.jsonl"


def key_for(judge: Judge, case: Case) -> str:
    return request_key(LlmDecisionModel.backend, judge.name, request_for(case, True))


async def run(judge_key: str, limit: int | None) -> None:
    judge = resolve_judge(judge_key)
    concurrency, gap = PACING[judge_key]
    policy = start_policy()
    cache = DecisionCache(cache_path(judge_key))
    pending = [
        case
        for case in all_cases()
        if not carries_secret(case, policy) and cache.get(key_for(judge, case)) is None
    ]
    if limit is not None:
        pending = pending[:limit]
    print(f"{judge.name}: {len(cache)} answers cached, {len(pending)} to ask", flush=True)

    model = LlmDecisionModel.for_judge(judge, retry=RetryPolicy(max_retries=5))
    slots = asyncio.Semaphore(concurrency)
    start_lock = asyncio.Lock()
    last_start = 0.0
    tokens = {"input": 0, "output": 0}
    failures: list[dict] = []
    done = 0

    async def ask(case: Case, timings) -> None:
        nonlocal last_start, done
        async with slots:
            async with start_lock:
                wait = last_start + gap - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                last_start = time.monotonic()
            started = time.perf_counter()
            try:
                decision = await model.decide(request_for(case, True))
            except DecisionError as error:
                failures.append({"case": case["id"], "error": f"{error.kind}: {error}"[:300]})
                return
            finally:
                done += 1
                if done % 50 == 0:
                    print(f"  {done} of {len(pending)} asked, tokens so far {tokens}", flush=True)
            latency_ms = (time.perf_counter() - started) * 1000
        cache.put(key_for(judge, case), decision)
        tokens["input"] += decision.input_tokens or 0
        tokens["output"] += decision.output_tokens or 0
        timings.write(json.dumps({"case": case["id"], "latency_ms": round(latency_ms, 1)}) + "\n")
        timings.flush()

    try:
        with timings_path(judge_key).open("a", encoding="utf-8", newline="\n") as timings:
            await asyncio.gather(*(ask(case, timings) for case in pending))
    finally:
        await model.aclose()

    if limit is None:
        with failures_path(judge_key).open("w", encoding="utf-8", newline="\n") as file:
            for failure in failures:
                file.write(json.dumps(failure) + "\n")
    print(
        f"{judge.name}: {len(cache)} answers in the cache, {len(failures)} failed this run. "
        f"Tokens this run: {tokens['input']} input, {tokens['output']} output."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("judge", choices=sorted(PACING))
    parser.add_argument("--limit", type=int, help="ask at most this many cases")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.judge, args.limit))
    except LlmConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
