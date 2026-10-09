"""M3 demo: Tollgate enforcing its policy on real upstreams.

Run with `uv run --env-file .env python scripts/enforce_demo.py` from the repo root.

The script plays the MCP client and the person at the approval prompt. It shows
a call being allowed, two being blocked by exact checks, one held until it is
approved, one held and denied, and one held because the model was given no time
to answer, which is what a Jev outage looks like to the gate.
"""

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml
from mcp import Client, StdioServerParameters

from tollgate.approve.queue import APPROVALS_FILE, ApprovalQueue
from tollgate.audit.report import render
from tollgate.audit.store import AuditStore
from tollgate.decide.jev import JevConfigError, read_api_key, resolve_route
from tollgate.decide.questions import USER_REQUEST_META
from tollgate.proxy.server import AUDIT_FILE

REPO = Path(__file__).resolve().parents[1]
SANDBOX = REPO / "examples" / "sandbox"
STATE_DIR = REPO / ".tollgate" / "demo"
POLICY = REPO / "policy.example.yaml"
OUTAGE_POLICY = STATE_DIR / "outage-policy.yaml"
SCRATCH = SANDBOX / "demo-scratch.txt"
TIMEOUT_SECONDS = 240

# Built at run time so that no key-shaped literal sits in the repository.
FAKE_ACCESS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
ASKED_TO_READ = {USER_REQUEST_META: "Read the hello file in the sandbox and tell me what it says."}
ASKED_TO_UPDATE = {USER_REQUEST_META: "Replace the text in demo-scratch.txt with a new line."}

# (what the call shows, tool, arguments, context, the answer if the call is held)
CALLS = [
    (
        "a read the user asked for",
        "read_text_file",
        {"path": str(SANDBOX / "hello.txt")},
        ASKED_TO_READ,
        True,
    ),
    (
        "a file outside the sandbox",
        "read_text_file",
        {"path": str(REPO / "README.md")},
        ASKED_TO_READ,
        False,
    ),
    (
        "content that holds an access key",
        "write_file",
        {"path": str(SCRATCH), "content": f"aws_access_key_id = {FAKE_ACCESS_KEY}\n"},
        ASKED_TO_UPDATE,
        False,
    ),
    (
        "an overwrite, which this script approves",
        "write_file",
        {"path": str(SCRATCH), "content": "Approved by the demo.\n"},
        ASKED_TO_UPDATE,
        True,
    ),
    (
        "another overwrite, which this script denies",
        "write_file",
        {"path": str(SCRATCH), "content": "This text must never be written.\n"},
        ASKED_TO_UPDATE,
        False,
    ),
]
OUTAGE_CALL = (
    "the read from call 1, with the model given 1 ms to answer",
    "read_text_file",
    {"path": str(SANDBOX / "hello.txt")},
    {USER_REQUEST_META: "What does the hello file in the sandbox say?"},
    False,
)


def approve_command(*arguments: str) -> str:
    """Run the real `tollgate-approve` command and return what it printed."""
    finished = subprocess.run(
        [sys.executable, "-m", "tollgate.approve.cli", "--state-dir", str(STATE_DIR), *arguments],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return (finished.stdout + finished.stderr).strip()


async def make_call(client: Client, number: int, call: tuple) -> list[str]:
    """Make one call through Tollgate, answering it if it is held; return what happened."""
    label, tool, arguments, meta, answer = call
    lines = [f"{number}. {tool}: {label}"]
    calling = asyncio.create_task(client.call_tool(tool, arguments, meta=meta))
    with ApprovalQueue(STATE_DIR / APPROVALS_FILE) as queue:
        while not calling.done():
            held = queue.pending()
            if held:
                verb = "approve" if answer else "deny"
                listing = await asyncio.to_thread(approve_command, "list")
                answered = await asyncio.to_thread(approve_command, verb, str(held[0].id))
                lines.append("   held. `tollgate-approve list` shows:")
                lines.extend(f"       {line}" for line in listing.splitlines())
                lines.append(f"   `tollgate-approve {verb} {held[0].id}` -> {answered}")
                break
            await asyncio.sleep(0.1)
    result = await calling
    text = " ".join(getattr(block, "text", "") for block in result.content).replace("\n", " ")
    lines.append(f"   -> {'refused' if result.is_error else 'ran'}: {text[:230]}")
    return lines


async def run(policy: Path, calls: list[tuple], first_number: int) -> list[str]:
    tollgate = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "tollgate.proxy.server",
            "--upstreams",
            "examples/upstreams.json",
            "--policy",
            str(policy),
            "--state-dir",
            str(STATE_DIR),
        ],
        cwd=str(REPO),
        env=dict(os.environ),
    )
    lines: list[str] = []
    async with Client(tollgate, cache=None) as client:
        for offset, call in enumerate(calls):
            lines.extend(await make_call(client, first_number + offset, call))
    return lines


def main() -> int:
    try:
        read_api_key(resolve_route())
    except JevConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    shutil.rmtree(STATE_DIR, ignore_errors=True)
    STATE_DIR.mkdir(parents=True)
    outage = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    outage["decision"]["timeout_ms"] = 1
    OUTAGE_POLICY.write_text(yaml.safe_dump(outage), encoding="utf-8")
    SCRATCH.write_text("Original text.\n", encoding="utf-8")

    try:
        normal = asyncio.run(asyncio.wait_for(run(POLICY, CALLS, 1), TIMEOUT_SECONDS))
        during_outage = asyncio.run(
            asyncio.wait_for(run(OUTAGE_POLICY, [OUTAGE_CALL], len(CALLS) + 1), TIMEOUT_SECONDS)
        )
        scratch_text = SCRATCH.read_text(encoding="utf-8")
    finally:
        SCRATCH.unlink(missing_ok=True)

    print("\nCalls made through Tollgate in enforce mode, with policy.example.yaml:\n")
    print("\n".join(normal))
    print("\nA simulated Jev outage (the same policy, with decision.timeout_ms set to 1):\n")
    print("\n".join(during_outage))
    print(f"\nThe scratch file ends up holding: {scratch_text.strip()!r}")
    with AuditStore(STATE_DIR / AUDIT_FILE) as store:
        records = store.recent(len(CALLS) + 1)
    print("\nAudit log:\n")
    print(render(records))
    print(f"\nThe log is at {STATE_DIR / AUDIT_FILE}")

    newest_first = {index: record for index, record in enumerate(reversed(records), 1)}
    checks = {
        "the out-of-sandbox read was blocked": newest_first[2].action == "blocked",
        "the access key was blocked and redacted": newest_first[3].action == "blocked"
        and FAKE_ACCESS_KEY not in str(newest_first[3].arguments),
        "the approved overwrite ran": scratch_text == "Approved by the demo.\n",
        "the denied overwrite did not run": newest_first[5].action == "denied",
        "the outage call was held, not allowed": newest_first[6].band == "ask"
        and newest_first[6].action == "denied"
        and (newest_first[6].error or "").startswith("timeout"),
    }
    print()
    for claim, holds in checks.items():
        print(f"{'ok  ' if holds else 'FAIL'}  {claim}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
