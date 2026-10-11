import json

import pytest

from tollgate.proxy.upstreams import (
    HttpUpstream,
    StdioUpstream,
    UpstreamConfigError,
    load_upstreams,
    parse_upstreams,
    upstream_environment,
)


def test_entry_with_a_command_is_a_stdio_upstream():
    upstreams = parse_upstreams(
        {
            "mcpServers": {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
                    "env": {"DEBUG": "1"},
                }
            }
        },
        env={},
    )

    assert upstreams == {
        "filesystem": StdioUpstream(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "."],
            env={"DEBUG": "1"},
        )
    }


def test_entry_with_a_url_is_an_http_upstream():
    upstreams = parse_upstreams(
        {"mcpServers": {"remote": {"type": "http", "url": "https://example.com/mcp"}}},
        env={},
    )

    assert upstreams == {"remote": HttpUpstream(url="https://example.com/mcp")}


def test_upstreams_keep_the_order_of_the_file():
    upstreams = parse_upstreams(
        {"mcpServers": {"b": {"command": "b"}, "a": {"command": "a"}}},
        env={},
    )

    assert list(upstreams) == ["b", "a"]


def test_variables_are_expanded_from_the_environment():
    upstreams = parse_upstreams(
        {
            "mcpServers": {
                "remote": {
                    "url": "https://${HOST}/mcp",
                    "headers": {"Authorization": "Bearer ${TOKEN}"},
                },
                "local": {"command": "server", "args": ["--root", "${ROOT:-/srv}"]},
            }
        },
        env={"HOST": "example.com", "TOKEN": "abc"},
    )

    assert upstreams["remote"] == HttpUpstream(
        url="https://example.com/mcp", headers={"Authorization": "Bearer abc"}
    )
    assert upstreams["local"] == StdioUpstream(command="server", args=["--root", "/srv"])


def test_a_missing_variable_names_the_variable_and_the_upstream():
    with pytest.raises(UpstreamConfigError, match=r"mcpServers\.remote.*TOKEN is not set"):
        parse_upstreams(
            {"mcpServers": {"remote": {"url": "https://x", "headers": {"A": "${TOKEN}"}}}},
            env={},
        )


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({}, "mcpServers"),
        ({"mcpServers": {}}, "at least one upstream"),
        ({"mcpServers": {"x": {"args": []}}}, r"mcpServers\.x.*command.*url"),
        ({"mcpServers": {"x": {"command": "a", "url": "https://x"}}}, r"mcpServers\.x"),
        ({"mcpServers": {"x": {"command": "a", "disabled": True}}}, r"mcpServers\.x.*disabled"),
        ({"mcpServers": {"x": {"type": "sse", "url": "https://x"}}}, r"mcpServers\.x.*type"),
        ({"mcpServers": {"x": "npx"}}, r"mcpServers\.x"),
    ],
    ids=[
        "no-mcpServers",
        "empty",
        "neither-command-nor-url",
        "both-command-and-url",
        "unknown-key",
        "sse-not-supported",
        "entry-not-an-object",
    ],
)
def test_invalid_documents_are_rejected(document, message):
    with pytest.raises(UpstreamConfigError, match=message):
        parse_upstreams(document, env={})


def test_load_reads_a_json_file(tmp_path):
    path = tmp_path / "upstreams.json"
    path.write_text(json.dumps({"mcpServers": {"x": {"command": "a"}}}), encoding="utf-8")

    assert load_upstreams(path, env={}) == {"x": StdioUpstream(command="a")}


def test_load_reports_a_missing_file_and_bad_json(tmp_path):
    with pytest.raises(UpstreamConfigError, match="cannot read"):
        load_upstreams(tmp_path / "absent.json", env={})

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(UpstreamConfigError, match="not valid JSON"):
        load_upstreams(broken, env={})


def test_a_stdio_upstream_inherits_the_environment_without_tollgates_keys():
    environ = {
        "PATH": "/bin",
        "NODIST_PREFIX": "C:/nodist",
        "TYPESAFE_API_KEY": "jev-secret",
        "OPENROUTER_API_KEY": "router-secret",
        "GEMINI_API_KEY": "judge-secret",
    }

    started_with = upstream_environment(StdioUpstream(command="npx"), environ)

    assert started_with == {"PATH": "/bin", "NODIST_PREFIX": "C:/nodist"}


def test_the_upstreams_file_can_set_and_override_variables():
    config = StdioUpstream(command="server", env={"MODE": "test", "TYPESAFE_API_KEY": "passed-on"})

    started_with = upstream_environment(config, {"MODE": "prod", "TYPESAFE_API_KEY": "jev-secret"})

    assert started_with == {"MODE": "test", "TYPESAFE_API_KEY": "passed-on"}
