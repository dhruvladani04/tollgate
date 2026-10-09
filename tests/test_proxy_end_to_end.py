"""The real `tollgate` process, reached over stdio, in front of real upstream processes."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import fake_upstream
import pytest
import yaml
from mcp import Client, StdioServerParameters

from tollgate.approve.queue import APPROVALS_FILE, ApprovalQueue
from tollgate.audit.store import AuditStore

FAKE = str(Path(__file__).with_name("fake_upstream.py"))
SERVER = [sys.executable, "-m", "tollgate.proxy.server"]
TOLLGATE = [*SERVER, "--mode", "passthrough"]
TIMEOUT_SECONDS = 60
EXAMPLE_POLICY = Path(__file__).resolve().parents[1] / "policy.example.yaml"


def stdio_upstream(flavor: str) -> dict:
    return {"command": sys.executable, "args": [FAKE, flavor]}


def write_config(directory: Path, servers: dict) -> Path:
    path = directory / "upstreams.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def run_tollgate(config: Path) -> subprocess.CompletedProcess[str]:
    """Run Tollgate with no client attached; it exits at once when startup fails."""
    return subprocess.run(
        [*TOLLGATE, "--upstreams", str(config)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )


@pytest.fixture
def http_upstream() -> Iterator[str]:
    """The math fake served over streamable HTTP; yields its URL."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, FAKE, "math", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("the HTTP fake upstream did not start") from None
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        process.wait(timeout=TIMEOUT_SECONDS)


def test_a_client_lists_and_calls_tools_from_a_stdio_and_an_http_upstream(tmp_path, http_upstream):
    config = write_config(
        tmp_path,
        {"text": stdio_upstream("text"), "math": {"type": "http", "url": http_upstream}},
    )
    tollgate = StdioServerParameters(
        command=TOLLGATE[0], args=[*TOLLGATE[1:], "--upstreams", str(config)]
    )

    async def through_tollgate():
        async with Client(tollgate, cache=None) as client:
            return (
                client.server_info.name,
                (await client.list_tools()).tools,
                await client.call_tool("echo", {"text": "héllo → world"}),
                await client.call_tool("add", {"a": 2, "b": 3}),
                await client.call_tool("fail", {"reason": "disk full"}),
            )

    async def direct():
        async with (
            Client(fake_upstream.build("text"), cache=None) as text,
            Client(fake_upstream.build("math"), cache=None) as math,
        ):
            return (
                (await text.list_tools()).tools + (await math.list_tools()).tools,
                await text.call_tool("echo", {"text": "héllo → world"}),
                await math.call_tool("add", {"a": 2, "b": 3}),
                await text.call_tool("fail", {"reason": "disk full"}),
            )

    server_name, *proxied = asyncio.run(asyncio.wait_for(through_tollgate(), TIMEOUT_SECONDS))
    expected = asyncio.run(direct())

    assert server_name == "tollgate"
    assert proxied == list(expected)


def test_an_upstream_process_sees_the_environment_but_not_tollgates_key(tmp_path):
    config = write_config(tmp_path, {"env": stdio_upstream("env")})
    tollgate = StdioServerParameters(
        command=TOLLGATE[0],
        args=[*TOLLGATE[1:], "--upstreams", str(config)],
        env={"TOLLGATE_TEST_MARKER": "visible", "TYPESAFE_API_KEY": "jev-secret"},
    )

    async def lookups():
        async with Client(tollgate, cache=None) as client:
            marker = await client.call_tool("lookup", {"name": "TOLLGATE_TEST_MARKER"})
            key = await client.call_tool("lookup", {"name": "TYPESAFE_API_KEY"})
            return marker.structured_content, key.structured_content

    marker, key = asyncio.run(asyncio.wait_for(lookups(), TIMEOUT_SECONDS))

    assert marker == {"result": "visible"}
    assert key == {"result": ""}


def test_startup_stops_when_two_upstreams_offer_the_same_tool(tmp_path):
    config = write_config(
        tmp_path, {"first": stdio_upstream("text"), "second": stdio_upstream("text")}
    )

    finished = run_tollgate(config)

    assert finished.returncode == 1
    assert "tool 'echo' is offered by both 'first' and 'second'" in finished.stderr


def test_startup_stops_when_an_upstream_cannot_be_started(tmp_path):
    config = write_config(tmp_path, {"ghost": {"command": "tollgate-no-such-command"}})

    finished = run_tollgate(config)

    assert finished.returncode == 1
    assert "tollgate: upstream 'ghost' failed to start" in finished.stderr


def test_startup_stops_on_an_unreadable_upstreams_file(tmp_path):
    finished = run_tollgate(tmp_path / "absent.json")

    assert finished.returncode == 1
    assert "tollgate: cannot read" in finished.stderr


def run_in_mode(config: Path, *options: str) -> subprocess.CompletedProcess[str]:
    """Run Tollgate with the given options and no Jev key in its environment."""
    keys = ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "TOLLGATE_JEV_ROUTE")
    env = {name: value for name, value in os.environ.items() if name not in keys}
    return subprocess.run(
        [*SERVER, "--upstreams", str(config), *options],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env=env,
    )


@pytest.mark.parametrize("mode", [["--mode", "observe"], ["--mode", "enforce"], []])
def test_a_gating_mode_without_a_key_stops_before_starting_any_upstream(tmp_path, mode):
    config = write_config(tmp_path, {"ghost": {"command": "tollgate-no-such-command"}})

    finished = run_in_mode(config, *mode, "--policy", str(EXAMPLE_POLICY))

    assert finished.returncode == 1
    assert "tollgate: TYPESAFE_API_KEY is not set" in finished.stderr
    assert "ghost" not in finished.stderr


def test_a_gating_mode_without_a_policy_file_says_so(tmp_path):
    config = write_config(tmp_path, {"text": stdio_upstream("text")})

    finished = run_in_mode(config, "--policy", str(tmp_path / "absent.yaml"))

    assert finished.returncode == 1
    assert "tollgate: cannot read the policy file" in finished.stderr


def test_enforce_mode_holds_a_call_it_cannot_judge_until_the_approval_command_approves_it(
    tmp_path,
):
    """The whole loop in real processes: client, Tollgate, upstream and approval command.

    The policy gives the model one millisecond, so every decision times out, as in an outage.
    """
    rules = yaml.safe_load(EXAMPLE_POLICY.read_text(encoding="utf-8"))
    rules["decision"]["timeout_ms"] = 1
    rules["prefilter"] |= {"allow_tools": [], "deny_tools": ["fail"]}
    rules["prefilter"]["path_sandbox"]["roots"] = []
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(yaml.safe_dump(rules), encoding="utf-8")
    config = write_config(tmp_path, {"text": stdio_upstream("text")})
    state = tmp_path / "state"
    tollgate = StdioServerParameters(
        command=SERVER[0],
        args=[
            *SERVER[1:],
            "--upstreams",
            str(config),
            "--policy",
            str(policy_file),
            "--state-dir",
            str(state),
        ],
        env={"TYPESAFE_API_KEY": "not-a-real-key"},
    )

    async def scenario():
        async with Client(tollgate, cache=None) as client:
            blocked = await client.call_tool("fail", {"reason": "never runs"})
            calling = asyncio.create_task(client.call_tool("echo", {"text": "hello"}))
            with ApprovalQueue(state / APPROVALS_FILE) as queue:
                while not queue.pending():
                    assert not calling.done(), "the call finished without being held"
                    await asyncio.sleep(0.05)
                [held] = queue.pending()
            await asyncio.sleep(0.3)
            still_held = not calling.done()
            approved = await asyncio.to_thread(
                subprocess.run,
                [
                    sys.executable,
                    "-m",
                    "tollgate.approve.cli",
                    "--state-dir",
                    str(state),
                    "approve",
                    str(held.id),
                ],
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )
            return blocked, held, still_held, approved, await calling

    blocked, held, still_held, approved, result = asyncio.run(
        asyncio.wait_for(scenario(), TIMEOUT_SECONDS)
    )

    assert blocked.is_error
    assert "deny list" in blocked.content[0].text
    assert held.tool == "echo"
    assert any("no decision from the model" in reason for reason in held.reasons)
    assert still_held
    assert approved.returncode == 0
    assert f"approved #{held.id} (echo)" in approved.stdout
    assert not result.is_error
    assert result.content[0].text == "hello"
    with AuditStore(state / "audit.sqlite") as audit:
        judged, refused = audit.recent(10)
    assert (refused.tool, refused.band, refused.action) == ("fail", "block", "blocked")
    assert (judged.tool, judged.band, judged.action) == ("echo", "ask", "forwarded")
    assert judged.error.startswith("timeout")
