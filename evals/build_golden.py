"""Build evals/data/golden_calls.jsonl from the scenes.

Run with `python -m evals.build_golden`. The output is committed, so the data an
evaluation ran on is fixed even if the scenes are edited later.
"""

import json
from collections import Counter
from pathlib import Path

from evals.golden import expand
from evals.scenes import FAMILIES

GOLDEN_CALLS = Path(__file__).parent / "data" / "golden_calls.jsonl"


def build() -> list[dict]:
    cases = []
    for family, module in FAMILIES.items():
        cases.extend(expand(module.SCENES, family, module.TOOLS))
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("two cases share an id")
    return cases


def main() -> None:
    cases = build()
    GOLDEN_CALLS.parent.mkdir(parents=True, exist_ok=True)
    with GOLDEN_CALLS.open("w", encoding="utf-8", newline="\n") as file:
        for case in cases:
            file.write(json.dumps(case, ensure_ascii=False) + "\n")

    def kind(case: dict) -> str:
        return "malicious" if case["malicious"] else "risky" if case["risky"] else "benign"

    print(f"wrote {len(cases)} cases to {GOLDEN_CALLS}")
    for name, key in (
        ("family", lambda c: c["family"]),
        ("split", lambda c: c["split"]),
        ("kind", kind),
        ("split and kind", lambda c: f"{c['split']} {kind(c)}"),
    ):
        counts = Counter(key(case) for case in cases)
        print(f"  by {name}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
