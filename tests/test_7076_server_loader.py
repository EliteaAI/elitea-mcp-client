import asyncio
import os
import time
from unittest.mock import patch

import pytest

from src.elitea_mcp.config_model import check_config
from src.elitea_mcp.utils import server_loader
from src.elitea_mcp.utils.server_loader import (AUTH, COMMAND_NOT_FOUND, EXITED, TIMEOUT, UNREACHABLE,
                                                collect_secret_values, load_servers, mask_secrets, mask_url_secrets)
from src.elitea_mcp.utils.server_registry import ServerRegistry
from src.elitea_mcp.utils.server_supervisor import ServerSupervisor
from tests import fixtures_7076 as fixtures

STARTUP_TIMEOUT = 3


def load(servers, startup_timeout=STARTUP_TIMEOUT, list_timeout=30):
    return {result.name: result for result in asyncio.run(load_servers(servers, startup_timeout, list_timeout))}


@pytest.fixture
def unauthorized():
    server, url = fixtures.serve_unauthorized()
    yield url
    server.shutdown()


def test_a_healthy_server_loads_next_to_one_whose_command_is_missing(tmp_path):
    started = time.monotonic()

    results = load({"ok": fixtures.healthy(tmp_path), "github": fixtures.missing_command()})

    assert time.monotonic() - started < STARTUP_TIMEOUT
    assert [tool["name"] for tool in results["ok"].tools] == ["echo", "fail", "crash", "slow"]
    assert results["github"].failure.kind == COMMAND_NOT_FOUND


def test_a_spawn_error_from_the_sdk_cannot_hang_the_healthy_server_next_to_it(tmp_path):
    started = time.monotonic()

    with patch.object(server_loader, "find_missing_command", return_value=None):
        results = load({"ok": fixtures.healthy(tmp_path), "github": fixtures.missing_command()})

    assert time.monotonic() - started < STARTUP_TIMEOUT
    assert results["ok"].ok
    assert results["github"].failure.kind == COMMAND_NOT_FOUND


def test_every_broken_server_is_reported_while_the_healthy_one_still_loads(tmp_path, unauthorized):
    results = load({
        "ok": fixtures.healthy(tmp_path),
        "missing": fixtures.missing_command(),
        "exits": fixtures.exits_at_start(),
        "hangs": fixtures.hangs(),
        "refused": fixtures.closed_port_url("http"),
        "refused_sse": fixtures.closed_port_url("sse"),
        "rejected": {"type": "http", "url": f"{unauthorized}/mcp", "headers": {}},
        "rejected_sse": {"type": "sse", "url": f"{unauthorized}/sse", "headers": {}},
    })

    assert results["ok"].ok
    assert {name: result.failure.kind for name, result in results.items() if not result.ok} == {
        "missing": COMMAND_NOT_FOUND, "exits": EXITED, "hangs": TIMEOUT, "refused": UNREACHABLE,
        "refused_sse": UNREACHABLE, "rejected": AUTH, "rejected_sse": AUTH,
    }


def test_a_hanging_server_times_out_on_its_own_startup_timeout_and_is_named(tmp_path):
    started = time.monotonic()

    results = load({"ok": fixtures.healthy(tmp_path), "figma": {**fixtures.hangs(), "startup_timeout": 1}},
                   startup_timeout=20)

    assert time.monotonic() - started < 10
    assert results["ok"].ok
    assert results["figma"].failure.describe("figma", "stdio") == (
        'Server "figma": did not respond within 1 s. Check the command/URL or increase "startup_timeout".')


@pytest.mark.parametrize("command, install", [("npx", "Install Node.js"), ("uvx", "Install uv")])
def test_a_missing_command_names_the_server_the_command_and_what_to_install(command, install):
    with patch.object(server_loader.shutil, "which", return_value=None):
        results = load({"github": {"type": "stdio", "command": command, "args": []}})

    assert results["github"].failure.describe("github", "stdio") == (
        f'Server "github": command "{command}" not found. {install} or use the full path in "command".')


def test_a_missing_command_is_named_before_spawning_so_windows_never_shows_a_bare_errno():
    with patch.object(server_loader, "stdio_client") as spawn:
        results = load({"github": fixtures.missing_command()})

    spawn.assert_not_called()
    assert results["github"].failure.reason == 'command "elitea-test-command-that-does-not-exist" not found'


def test_the_command_is_looked_up_on_the_path_the_server_is_started_with(tmp_path):
    fake = tmp_path / "bin" / "mytool"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)

    assert server_loader.find_missing_command({"command": "mytool", "env": {"PATH": str(fake.parent)}}) is None
    assert server_loader.find_missing_command({"command": "mytool"}).kind == COMMAND_NOT_FOUND


def test_an_exiting_server_keeps_its_last_output_apart_from_the_reason():
    results = load({"jira": fixtures.exits_at_start("boom: missing GITHUB_TOKEN")})

    assert results["jira"].failure.describe("jira", "stdio") == (
        'Server "jira": exited during start. Check its "command", "args" and "env".')
    assert results["jira"].stderr_tail == ["boom: missing GITHUB_TOKEN"]


def test_unreachable_and_rejected_urls_name_the_transport_and_the_fix(unauthorized):
    refused = fixtures.closed_port_url("sse")
    results = load({"confluence": refused, "jira": {"type": "http", "url": f"{unauthorized}/mcp", "headers": {}}})

    assert results["confluence"].failure.describe("confluence", "sse") == (
        f'Server "confluence" (sse): cannot reach {refused["url"]}: connection refused. '
        'Check "url" and that the server is running.')
    assert results["jira"].failure.describe("jira", "http") == (
        'Server "jira" (http): 401 Unauthorized. Check the token in "headers".')


def test_configured_secrets_are_masked_in_server_output():
    config = {"auth_token": "platform-token-123"}
    server_conf = {"env": {"GITHUB_TOKEN": "ghp_secret", "DEBUG": "1"}, "headers": {"Authorization": "Bearer abcd1234"}}

    masked = mask_secrets(["token ghp_secret used", "auth abcd1234 and platform-token-123", "DEBUG=1"],
                          collect_secret_values(config, server_conf))

    assert masked == ["token **** used", "auth **** and ****", "DEBUG=1"]


def test_startup_timeout_is_a_setting_globally_and_per_server():
    report = check_config({"startup_timeout": 45, "servers": {"a": {"command": "npx", "startup_timeout": 5},
                                                              "b": {"type": "http", "url": "http://x/mcp"}}})

    assert report.config["startup_timeout"] == 45
    assert report.config["servers"]["a"]["startup_timeout"] == 5
    assert report.config["servers"]["b"]["startup_timeout"] is None
    assert not [problem for problem in report.problems if "startup_timeout" in problem.path]


def test_startup_timeout_defaults_to_30_seconds_and_a_zero_one_skips_the_server():
    assert check_config({}).config["startup_timeout"] == 30
    assert check_config({"servers": {"a": {"command": "npx", "startup_timeout": 0}}}).skipped_servers == ["a"]


def test_an_sse_server_that_never_sends_its_endpoint_times_out_on_startup_timeout(tmp_path):
    server, url = fixtures.serve_silent_event_stream()
    started = time.monotonic()

    results = load({"ok": fixtures.healthy(tmp_path), "stuck": {"type": "sse", "url": f"{url}/sse", "headers": {}}},
                   startup_timeout=2)

    server.shutdown()
    assert time.monotonic() - started < 10
    assert results["ok"].ok
    assert results["stuck"].failure.kind == TIMEOUT


def test_the_start_deadline_never_cuts_a_running_session_short(tmp_path):
    async def call_after_the_deadline():
        async with server_loader.open_session(fixtures.healthy(tmp_path), startup_timeout=1) as session:
            await asyncio.sleep(1.5)
            return (await session.call_tool("echo", {"text": "still here"})).content[0].text

    assert asyncio.run(call_after_the_deadline()) == "still here"


def test_on_windows_the_command_is_looked_up_on_the_clients_path_as_the_sdk_does(tmp_path):
    fake = tmp_path / "bin" / "mytool"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    server_conf = {"command": "mytool", "env": {"PATH": str(tmp_path / "elsewhere")}}

    with patch.object(server_loader, "ON_WINDOWS", True), \
            patch.dict(server_loader.os.environ, {"PATH": str(fake.parent)}):
        assert server_loader.find_missing_command(server_conf) is None
    with patch.dict(server_loader.os.environ, {"PATH": str(fake.parent)}):
        assert server_loader.find_missing_command(server_conf).kind == COMMAND_NOT_FOUND


def test_a_stdio_server_that_times_out_leaves_none_of_its_children_running(tmp_path):
    pid_file = tmp_path / "child.pid"

    results = load({"wrapper": fixtures.forks_and_hangs(pid_file)}, startup_timeout=2)

    assert results["wrapper"].failure.kind == TIMEOUT
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(child, 9)
    pytest.fail(f"child process {child} of the timed-out server is still running")


@pytest.mark.parametrize("text, expected", [
    ("https://user:pa55w0rd@host.example:8443/mcp", "https://host.example:8443/mcp"),
    ("https://host.example/mcp?api_key=S1&region=eu&token=S2", "https://host.example/mcp?api_key=****&region=****&token=****"),
    ("https://host.example/mcp", "https://host.example/mcp"),
    ("cannot reach https://u:p@host.example/sse?key=S1: connection refused.", "cannot reach https://host.example/sse?key=****: connection refused."),
    ("https://host.example/a#access_token=S3", "https://host.example/a#access_token=****"),
])
def test_local_text_keeps_a_urls_host_and_path_but_not_its_secrets(text, expected):
    assert mask_url_secrets(text) == expected


def test_a_token_inside_the_url_path_stays_visible_locally_as_documented():
    assert mask_url_secrets("https://mcp.zapier.com/api/mcp/s/TOKEN123/mcp") == "https://mcp.zapier.com/api/mcp/s/TOKEN123/mcp"


def test_every_local_surface_shows_the_masked_url():
    url = f"http://user:pa55w0rd@127.0.0.1:{fixtures.find_closed_port()}/mcp?api_key=SECRET1"
    supervisor = ServerSupervisor({"auth_token": "t", "timeout": 30, "servers": {"remote": {"type": "http", "url": url, "headers": {}}}})

    supervisor.load()

    entry = supervisor.registry.get("remote")
    for text in (entry.message, entry.format_menu_row(), entry.describe_status(), supervisor.format_error_report("remote")):
        assert "SECRET1" not in text and "pa55w0rd" not in text
        assert "/mcp?api_key=****" in text


def test_server_output_shown_locally_has_its_urls_masked_too():
    assert mask_secrets(["GET https://host.example/mcp?api_key=SECRET1 failed"], []) == ["GET https://host.example/mcp?api_key=**** failed"]


@pytest.mark.parametrize("text, expected", [
    ("proxy target: [http://localhost:3000]", "proxy target: [http://localhost:3000]"),
    ("usage: --url http://[host]:port/mcp", "usage: --url http://[host]:port"),
    ("http://[::1]:8080/mcp?token=abc", "http://[::1]:8080/mcp?token=****"),
    ("see [https://a](https://u:p@b/x?token=SECRET)", "see [https://a](https:"),
])
def test_text_with_a_url_that_does_not_parse_is_masked_without_raising(text, expected):
    masked = mask_url_secrets(text)

    assert masked.startswith(expected)
    assert "SECRET" not in masked and "u:p@" not in masked


def test_a_failing_servers_bracketed_url_in_its_output_never_stops_the_other_servers(tmp_path):
    servers = {"ok": fixtures.healthy(tmp_path), "proxy": fixtures.exits_at_start("proxy target: [http://localhost:3000]")}
    supervisor = ServerSupervisor({"auth_token": "t", "timeout": 30, "startup_timeout": 5, "servers": servers})

    supervisor.load()

    assert supervisor.registry.get("ok").status == "healthy"
    assert "proxy target: [http://localhost:3000]" in supervisor.format_error_report("proxy")
    assert supervisor.read_console_output("proxy") == ["proxy target: [http://localhost:3000]"]
