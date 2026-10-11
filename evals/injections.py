"""The data for the result scan, and the models' answers about it.

The texts are deepset's prompt-injections set (Apache 2.0,
https://huggingface.co/datasets/deepset/prompt-injections), which the build
plan names and an independent evaluation of Jev also used. Each text is put to
the model as a tool result, with the gate's own `injected` question. The set's
own train and test split is kept.

The texts are prompts written to a chatbot, not text that tools returned, so
this measures the question on a neighbouring task. The report says so.

    python -m evals.injections build              download the texts (no key)
    python -m evals.injections jev                ask Jev about every text
    python -m evals.injections gemini-3.8-flash   ask a judge about the test split
"""

import argparse
import asyncio
import json
import sys
import urllib.parse
import urllib.request

from typesafe_sdk import RetryPolicy, TypeSafeError

from evals.data import BACKEND, CACHE, EVALS, MODEL
from evals.judge import PACING, cache_path
from tollgate.decide.base import DecisionError, DecisionRequest
from tollgate.decide.cache import DecisionCache, request_key
from tollgate.decide.jev import JevConfigError, JevDecisionModel, make_client, resolve_route
from tollgate.decide.llm import LlmConfigError, LlmDecisionModel, resolve_judge
from tollgate.decide.questions import result_request

DATASET = "deepset/prompt-injections"
# The dataset revision the committed file was built from.
REVISION = "4f61ecb038e9c3fb77e21034b22511b523772cdd"
ROWS_API = "https://datasets-server.huggingface.co/rows"
PAGE = 100
INJECTIONS = EVALS / "data" / "external" / "prompt_injections.jsonl"
# The gate scans with the example policy's limit; no text in the set comes near it.
CONCURRENCY = 6


def load_texts() -> list[dict]:
    return [json.loads(line) for line in INJECTIONS.read_text(encoding="utf-8").splitlines()]


def scan_request(entry: dict) -> DecisionRequest:
    """The exact request the gate would send to scan this text."""
    return result_request(entry["text"])


def jev_key(entry: dict) -> str:
    return request_key(BACKEND, MODEL, scan_request(entry))


def judge_key(judge_name: str, entry: dict) -> str:
    return request_key(LlmDecisionModel.backend, judge_name, scan_request(entry))


def build() -> None:
    entries = []
    for split in ("train", "test"):
        offset = 0
        while True:
            query = urllib.parse.urlencode(
                {
                    "dataset": DATASET,
                    "config": "default",
                    "split": split,
                    "offset": offset,
                    "length": PAGE,
                }
            )
            with urllib.request.urlopen(f"{ROWS_API}?{query}", timeout=60) as response:
                rows = json.load(response)["rows"]
            for row in rows:
                entries.append(
                    {
                        "id": f"pi-{split}-{row['row_idx']:03d}",
                        "split": split,
                        "text": row["row"]["text"],
                        "injected": bool(row["row"]["label"]),
                    }
                )
            if len(rows) < PAGE:
                break
            offset += PAGE
    with INJECTIONS.open("w", encoding="utf-8", newline="\n") as file:
        for entry in entries:
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    positives = sum(entry["injected"] for entry in entries)
    print(f"wrote {len(entries)} texts to {INJECTIONS}: {positives} labelled as injections")


async def ask_all(model, cache: DecisionCache, pending: dict, concurrency: int, gap: float) -> int:
    slots = asyncio.Semaphore(concurrency)
    failed = 0

    async def ask(key: str, request: DecisionRequest, index: int) -> None:
        nonlocal failed
        await asyncio.sleep(index * gap)
        async with slots:
            try:
                cache.put(key, await model.decide(request))
            except DecisionError as error:
                failed += 1
                print(f"  no answer: {error.kind}: {str(error)[:120]}", flush=True)

    await asyncio.gather(
        *(ask(key, request, index) for index, (key, request) in enumerate(pending.items()))
    )
    return failed


async def ask_jev() -> None:
    route = resolve_route()
    cache = DecisionCache(CACHE)
    pending = {
        key: scan_request(entry)
        for entry in load_texts()
        if cache.get(key := jev_key(entry)) is None
    }
    async with make_client(route, retry=RetryPolicy(max_retries=4)) as client:
        failed = await ask_all(
            JevDecisionModel(client, route.model), cache, pending, CONCURRENCY, 0.0
        )
    print(f"{route.model}: {len(pending)} texts asked, {failed} without an answer")


async def ask_judge(name: str) -> None:
    judge = resolve_judge(name)
    concurrency, gap = PACING[name]
    cache = DecisionCache(cache_path(name))
    pending = {
        key: scan_request(entry)
        for entry in load_texts()
        if entry["split"] == "test" and cache.get(key := judge_key(judge.name, entry)) is None
    }
    model = LlmDecisionModel.for_judge(judge, retry=RetryPolicy(max_retries=5))
    try:
        failed = await ask_all(model, cache, pending, concurrency, gap)
    finally:
        await model.aclose()
    print(f"{judge.name}: {len(pending)} test texts asked, {failed} without an answer")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["build", "jev", *sorted(PACING)])
    action = parser.parse_args().action
    try:
        if action == "build":
            build()
        elif action == "jev":
            asyncio.run(ask_jev())
        else:
            asyncio.run(ask_judge(action))
    except (JevConfigError, LlmConfigError, TypeSafeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
