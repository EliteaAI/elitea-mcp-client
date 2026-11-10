import asyncio
import re
import socket
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import socketio

from src.elitea_mcp.utils import sio as sio_module
from src.elitea_mcp.utils.client_identity import read_machine_part
from src.elitea_mcp.utils.server_loader import (EXITED, INVALID_CONFIG, TIMEOUT, UNREACHABLE, ServerFailure, ServerLoad,
                                                load_servers, read_startup_timeout, redact_urls)
from src.elitea_mcp.utils.server_registry import DECLINED, FAILED, HEALTHY, REGISTERED, ServerRegistry
from src.elitea_mcp.utils.sio import start_socket_connection
from tests import fixtures_7076 as fixtures
from tests.test_7070_platform_registration import drop_platform, find_free_port, serve_platform, wait_until

TOOL = {"name": "echo", "description": "", "inputSchema": {"type": "object", "properties": {}, "required": []}}
UUID7 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def protocol_2_ack(data):
    names = [toolkit["name"] for toolkit in data["toolkit_configs"]]
    return {"ok": True, "protocol": 2, "epoch": data["epoch"], "generation": data["generation"], "stale": False,
            "registered": names, "declined": [], "failed": [item["name"] for item in data["failed_servers"]]}


def legacy_ack(data):
    return {"ok": True, "registered": [toolkit["name"] for toolkit in data["toolkit_configs"]], "declined": []}


class Platform:
    """A socket.io server that records every mcp_connect and answers with `self.answer(data)`."""

    def __init__(self, answer):
        self.answer = answer
        self.payloads = []
        self.sids = []
        self.port = find_free_port()
        self.start()

    def start(self):
        self.stopped = False
        self.httpd, self.server = serve_platform(self.port, self.on_mcp_connect)
        self.server.on("connect", lambda sid, environ: self.sids.append(sid))

    def on_mcp_connect(self, sid, data):
        self.payloads.append(data)
        return self.answer(data)

    def call_tool(self, server, tool="echo", arguments=None):
        return self.server.call("mcp_tools_call", {"server": server, "params": {"name": tool, "arguments": arguments or {"text": "hi"}}},
                                to=self.sids[-1], timeout=60)

    def config(self, servers=None):
        return {"deployment_url": f"http://127.0.0.1:{self.port}", "auth_token": "token", "project_id": 13,
                "timeout": 30, "startup_timeout": 5, "servers": servers or {}}

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        drop_platform(self.httpd, self.server)

    def close(self, link):
        """Cut the client off instead of a clean disconnect, which waits out a whole long-poll after a platform call."""
        link.sio.reconnection = False
        self.stop()
        wait_until(lambda: not link.connected)


@pytest.fixture(params=["protocol_2", "legacy"])
def platform(request):
    platform = Platform(protocol_2_ack if request.param == "protocol_2" else legacy_ack)
    platform.protocol = request.param
    yield platform
    platform.stop()


@pytest.fixture
def legacy_platform():
    platform = Platform(legacy_ack)
    yield platform
    platform.stop()


@pytest.fixture
def protocol_2_platform():
    platform = Platform(protocol_2_ack)
    yield platform
    platform.stop()


def registry_with(healthy=(), failed=()):
    registry = ServerRegistry()
    for name in healthy:
        registry.record_load(ServerLoad(name, "stdio", tools=[TOOL]))
    for name in failed:
        registry.record_load(ServerLoad(name, "stdio", failure=ServerFailure(EXITED, "exited during start", "Check it.")))
    return registry


def connect(platform, registry, servers=None, **callbacks):
    link = start_socket_connection(platform.config(servers), registry, **callbacks)
    link.sio.reconnection_delay = 0.2
    link.sio.reconnection_delay_max = 0.3
    link.sio.randomization_factor = 0
    return link


def test_the_registration_carries_identity_ordering_and_failures_but_only_healthy_toolkits(platform):
    link = connect(platform, registry_with(healthy=["github"], failed=["jira"]))

    payload = platform.payloads[0]
    assert [toolkit["name"] for toolkit in payload["toolkit_configs"]] == ["github"]
    assert payload["failed_servers"] == [{"name": "jira", "transport": "stdio", "kind": EXITED,
                                          "message": 'Server "jira": exited during start. Check it.'}]
    assert payload["protocol"] == 2
    assert payload["client_id"].startswith(f"{read_machine_part()}-")
    assert UUID7.match(payload["epoch"])
    assert isinstance(payload["generation"], int)
    link.disconnect()


def test_every_snapshot_has_a_higher_generation_in_the_same_epoch(protocol_2_platform):
    link = connect(protocol_2_platform, registry_with(healthy=["github"]))

    link.sync()
    link.sync()

    first, second, third = protocol_2_platform.payloads
    assert first["generation"] < second["generation"] < third["generation"]
    assert first["epoch"] == second["epoch"] == third["epoch"]
    link.disconnect()


def test_the_client_id_survives_a_restart_and_is_kept_in_its_own_file(protocol_2_platform, tmp_path):
    first = connect(protocol_2_platform, registry_with(healthy=["github"]))
    first.disconnect()
    second = connect(protocol_2_platform, registry_with(healthy=["github"]))

    assert protocol_2_platform.payloads[0]["client_id"] == protocol_2_platform.payloads[1]["client_id"]
    assert (tmp_path / "client_id").read_text() == protocol_2_platform.payloads[0]["client_id"]
    second.disconnect()


def test_a_client_id_copied_from_another_host_is_replaced(protocol_2_platform, tmp_path):
    (tmp_path / "client_id").write_text("0123456789ab-copied")

    link = connect(protocol_2_platform, registry_with(healthy=["github"]))

    assert protocol_2_platform.payloads[0]["client_id"] != "0123456789ab-copied"
    assert protocol_2_platform.payloads[0]["client_id"].startswith(f"{read_machine_part()}-")
    link.disconnect()


def test_a_retry_on_an_old_platform_adds_only_the_recovered_servers(legacy_platform):
    registry = registry_with(healthy=["github"], failed=["jira"])
    link = connect(legacy_platform, registry)

    registry.record_load(ServerLoad("jira", "stdio", tools=[TOOL]))
    link.sync(["jira"])

    assert [toolkit["name"] for toolkit in legacy_platform.payloads[1]["toolkit_configs"]] == ["jira"]
    assert registry.get("jira").registration == REGISTERED
    link.disconnect()


def test_a_retry_on_a_protocol_2_platform_sends_the_whole_current_state(protocol_2_platform):
    registry = registry_with(healthy=["github"], failed=["jira"])
    link = connect(protocol_2_platform, registry)

    registry.record_load(ServerLoad("jira", "stdio", tools=[TOOL]))
    link.sync(["jira"])

    assert [toolkit["name"] for toolkit in protocol_2_platform.payloads[1]["toolkit_configs"]] == ["github", "jira"]
    assert protocol_2_platform.payloads[1]["failed_servers"] == []
    link.disconnect()


def test_an_old_platform_declining_a_name_this_client_already_holds_still_counts_as_registered(legacy_platform):
    registry = registry_with(healthy=["github", "jira"])
    link = connect(legacy_platform, registry)
    legacy_platform.answer = lambda data: {"ok": True, "registered": [],
                                           "declined": [{"name": "github", "reason": "name_already_registered"},
                                                        {"name": "jira", "reason": "name_already_registered"}]}
    registry.get("jira").registration = None

    link.sync(["github", "jira"])

    assert registry.get("github").registration == REGISTERED
    assert registry.get("jira").registration == DECLINED
    link.disconnect()


def test_a_stale_answer_changes_nothing(protocol_2_platform):
    registry = registry_with(healthy=["github"])
    link = connect(protocol_2_platform, registry)
    protocol_2_platform.answer = lambda data: {"ok": True, "protocol": 2, "stale": True, "registered": [],
                                               "declined": [{"name": "github", "reason": "name_already_registered"}]}

    link.sync()

    assert registry.get("github").registration == REGISTERED
    link.disconnect()


def test_a_reconnect_registers_the_current_servers_not_the_ones_from_startup(platform):
    registry = registry_with(healthy=["github"], failed=["jira"])
    notify_status = MagicMock()
    link = connect(platform, registry, notify_status=notify_status)
    registry.record_load(ServerLoad("jira", "stdio", tools=[TOOL]))

    platform.stop()
    wait_until(lambda: not link.connected)
    platform.start()
    wait_until(lambda: len(platform.payloads) == 2)

    assert [toolkit["name"] for toolkit in platform.payloads[1]["toolkit_configs"]] == ["github", "jira"]
    wait_until(lambda: [c.args[0] for c in notify_status.call_args_list].count("Connected to platform") == 2)
    link.disconnect()


@patch.object(sio_module, "REGISTRATION_TIMEOUT", 1)
def test_an_unanswered_snapshot_is_sent_again_with_the_next_generation(protocol_2_platform):
    link = connect(protocol_2_platform, registry_with(healthy=["github"]))
    answers = iter([lambda data: time.sleep(2), protocol_2_ack])
    protocol_2_platform.answer = lambda data: next(answers)(data)

    link.sync()

    first_try, resend = protocol_2_platform.payloads[1:]
    assert resend["generation"] == first_try["generation"] + 1
    assert link.connected
    link.disconnect()


def test_changes_that_arrive_during_a_send_are_merged_into_one_more_send(protocol_2_platform):
    link = connect(protocol_2_platform, registry_with(healthy=["github"]))
    in_flight, release = threading.Event(), threading.Event()

    def slow_answer(data):
        in_flight.set()
        release.wait(5)
        return protocol_2_ack(data)

    protocol_2_platform.answer = slow_answer
    sender = threading.Thread(target=link.sync)
    sender.start()
    in_flight.wait(5)
    for _ in range(3):
        link.sync()
    release.set()
    sender.join(10)

    assert len(protocol_2_platform.payloads) == 3
    link.disconnect()


def test_a_tool_call_returns_the_tool_output(protocol_2_platform, tmp_path):
    server = fixtures.healthy(tmp_path)
    link = connect(protocol_2_platform, registry_with(healthy=["ok"]), {"ok": server})

    assert protocol_2_platform.call_tool("ok") == "hi"
    protocol_2_platform.close(link)



def test_a_call_to_an_unknown_server_is_answered_with_a_named_error(protocol_2_platform):
    link = connect(protocol_2_platform, registry_with(healthy=["github"]), {"github": fixtures.missing_command()})

    result = protocol_2_platform.call_tool("jira")

    assert result == {"isError": True, "content": [{"type": "text", "text": 'Server "jira" is not running in this MCP client.'}]}
    protocol_2_platform.close(link)



def test_a_failing_tool_is_answered_at_once_and_never_counts_against_the_server(protocol_2_platform, tmp_path):
    registry = registry_with(healthy=["ok"])
    link = connect(protocol_2_platform, registry, {"ok": fixtures.healthy(tmp_path)})

    reported = protocol_2_platform.call_tool("ok", tool="fail")
    crashes = [protocol_2_platform.call_tool("ok", tool="crash") for _ in range(3)]

    assert reported["isError"] is True
    assert "the tool itself failed" in reported["content"][0]["text"]
    assert crashes[-1]["isError"] is True
    assert crashes[-1]["content"][0]["text"].startswith('Server "ok": tool "crash" failed:')
    assert registry.get("ok").status == HEALTHY
    protocol_2_platform.close(link)



def test_a_server_that_breaks_is_answered_at_once_and_marked_failed_on_the_second_failure(protocol_2_platform, tmp_path):
    server = fixtures.healthy(tmp_path)
    registry = registry_with(healthy=["ok"])
    on_server_failed = MagicMock()
    link = connect(protocol_2_platform, registry, {"ok": server}, on_server_failed=on_server_failed)
    fixtures.break_server(server)

    started = time.monotonic()
    first = protocol_2_platform.call_tool("ok")
    assert time.monotonic() - started < 10
    assert registry.get("ok").status == HEALTHY
    second = protocol_2_platform.call_tool("ok")

    expected = {"isError": True, "content": [{"type": "text", "text": 'Server "ok": exited during start. Check its "command", "args" and "env".'}]}
    assert first == second == expected
    assert registry.get("ok").status == FAILED
    assert registry.get("ok").stderr_tail == ["broken on purpose"]
    on_server_failed.assert_called_once_with("ok")
    protocol_2_platform.close(link)



def test_a_failed_server_is_answered_with_its_reason_without_being_started(protocol_2_platform, tmp_path):
    registry = registry_with(failed=["ok"], healthy=["github"])
    link = connect(protocol_2_platform, registry, {"ok": fixtures.healthy(tmp_path)})

    with patch.object(sio_module, "_mcp_tools_call_sync") as call:
        result = protocol_2_platform.call_tool("ok")

    call.assert_not_called()
    assert result["content"][0]["text"] == 'Server "ok": exited during start. Check it.'
    protocol_2_platform.close(link)



def test_a_success_between_two_transport_failures_resets_the_count(protocol_2_platform, tmp_path):
    server = fixtures.healthy(tmp_path)
    registry = registry_with(healthy=["ok"])
    link = connect(protocol_2_platform, registry, {"ok": server})

    fixtures.break_server(server)
    protocol_2_platform.call_tool("ok")
    fixtures.repair_server(server)
    assert protocol_2_platform.call_tool("ok") == "hi"
    fixtures.break_server(server)
    protocol_2_platform.call_tool("ok")

    assert registry.get("ok").status == HEALTHY
    protocol_2_platform.close(link)


def test_a_tool_list_refresh_answers_with_the_servers_that_load_and_skips_the_broken_one(protocol_2_platform, tmp_path):
    servers = {"ok": fixtures.healthy(tmp_path), "github": fixtures.missing_command()}
    link = connect(protocol_2_platform, registry_with(healthy=["ok"]), servers)

    answer = protocol_2_platform.server.call("mcp_tools_list", {}, to=protocol_2_platform.sids[-1], timeout=60)

    assert [toolkit["name"] for toolkit in answer["toolkit_configs"]] == ["ok"]
    protocol_2_platform.close(link)


@pytest.mark.parametrize("text, expected", [
    ("cannot reach https://mcp.zapier.com/api/mcp/s/SECRET123/mcp: connection refused.",
     "cannot reach https://mcp.zapier.com: connection refused."),
    ("https://user:pa55@host.example:8443/mcp?api_key=SECRET answered 404 Not Found", "https://host.example:8443 answered 404 Not Found"),
    ('Server "github": command "npx" not found.', 'Server "github": command "npx" not found.'),
])
def test_urls_keep_only_their_scheme_and_host(text, expected):
    assert redact_urls(text) == expected


def test_a_secret_in_a_failed_servers_url_never_reaches_the_platform(protocol_2_platform):
    registry = registry_with(healthy=["github"])
    secret_url = "https://mcp.zapier.com/api/mcp/s/SECRET123/mcp"
    registry.record_load(ServerLoad("zapier", "http", failure=ServerFailure(
        UNREACHABLE, f"cannot reach {secret_url}: connection refused", 'Check "url".')))
    link = connect(protocol_2_platform, registry, {"github": fixtures.missing_command(), "zapier": {"type": "http", "url": secret_url}})

    result = protocol_2_platform.call_tool("zapier")

    assert protocol_2_platform.payloads[0]["failed_servers"][0]["message"] == (
        'Server "zapier" (http): cannot reach https://mcp.zapier.com: connection refused. Check "url".')
    assert "SECRET123" not in result["content"][0]["text"]
    assert "SECRET123" in registry.get("zapier").message
    protocol_2_platform.close(link)


def test_a_server_that_hangs_at_start_is_marked_failed_even_when_both_timeouts_are_equal(protocol_2_platform):
    registry = registry_with(healthy=["hangs"])
    config = {**protocol_2_platform.config({"hangs": fixtures.hangs()}), "timeout": 3, "startup_timeout": 3}
    link = start_socket_connection(config, registry)

    first = protocol_2_platform.call_tool("hangs")
    protocol_2_platform.call_tool("hangs")

    assert first["content"][0]["text"] == 'Server "hangs": did not respond within 3 s. Check the command/URL or increase "startup_timeout".'
    assert registry.get("hangs").status == FAILED
    assert registry.get("hangs").failure.kind == TIMEOUT
    protocol_2_platform.close(link)


def test_a_slow_tool_times_out_without_counting_against_its_server(protocol_2_platform, tmp_path):
    registry = registry_with(healthy=["ok"])
    config = {**protocol_2_platform.config({"ok": fixtures.healthy(tmp_path)}), "timeout": 2, "startup_timeout": 5}
    link = start_socket_connection(config, registry)

    started = time.monotonic()
    results = [protocol_2_platform.call_tool("ok", tool="slow") for _ in range(2)]

    assert time.monotonic() - started < 2 * (config["timeout"] + 4)
    assert all(result["isError"] and 'tool "slow" failed' in result["content"][0]["text"] for result in results)
    assert registry.get("ok").status == HEALTHY
    protocol_2_platform.close(link)


def test_a_registered_remote_server_that_cannot_be_reached_answers_without_its_secret(protocol_2_platform):
    server = {**fixtures.closed_port_url("http"), "url": fixtures.closed_port_url("http")["url"].replace("/mcp", "/s/SECRET123/mcp")}
    link = connect(protocol_2_platform, registry_with(healthy=["zapier"]), {"zapier": server})

    result = protocol_2_platform.call_tool("zapier")

    assert "cannot reach http://127.0.0.1:" in result["content"][0]["text"]
    assert "SECRET123" not in result["content"][0]["text"]
    protocol_2_platform.close(link)


def test_a_retry_whose_platform_never_answered_is_sent_without_snapshot_identity(protocol_2_platform):
    protocol_2_platform.answer = lambda data: None
    registry = registry_with(healthy=["a", "b"], failed=["c"])
    link = connect(protocol_2_platform, registry)

    registry.record_load(ServerLoad("c", "stdio", tools=[TOOL]))
    link.sync(["c"])

    retry = protocol_2_platform.payloads[1]
    assert [toolkit["name"] for toolkit in retry["toolkit_configs"]] == ["c"]
    assert not {"client_id", "epoch", "generation"} & set(retry)
    assert {"client_id", "epoch", "generation"} <= set(protocol_2_platform.payloads[0])
    link.disconnect()


@patch.object(sio_module, "RESYNC_DELAY", 0.3)
def test_a_refused_update_during_the_session_keeps_the_socket_and_is_sent_again(protocol_2_platform):
    notify_status, notify_error = MagicMock(), MagicMock()
    link = connect(protocol_2_platform, registry_with(healthy=["github"]), notify_status=notify_status, notify_error=notify_error)
    answers = iter([lambda data: {"ok": False, "error": "rate_limited", "message": "Too many registrations."}] * 2)
    protocol_2_platform.answer = lambda data: next(answers, protocol_2_ack)(data)

    link.sync()
    wait_until(lambda: len(protocol_2_platform.payloads) == 4)

    assert link.connected
    notify_error.assert_not_called()
    assert [c.args[0] for c in notify_status.call_args_list].count(
        "Could not update the platform: Too many registrations. Retrying in 0.3 s.") == 1
    assert protocol_2_platform.payloads[3]["generation"] > protocol_2_platform.payloads[2]["generation"]
    link.disconnect()


@patch.object(sio_module, "REGISTRATION_TIMEOUT", 0.5)
@patch.object(sio_module, "SYNC_ATTEMPTS", 2)
@patch.object(sio_module, "RESYNC_DELAY", 30)
def test_an_update_the_platform_never_answers_keeps_the_socket(protocol_2_platform):
    notify_error = MagicMock()
    link = connect(protocol_2_platform, registry_with(healthy=["github"]), notify_error=notify_error)
    protocol_2_platform.answer = lambda data: time.sleep(1)

    link.sync()

    assert link.connected
    notify_error.assert_not_called()
    assert link._resync_timer is not None and link._resync_timer.is_alive()
    link.disconnect()
    assert not link._resync_timer.is_alive()


def test_what_a_server_writes_during_a_tool_call_still_reaches_the_console(tmp_path, capfd):
    server = fixtures.healthy(tmp_path)

    assert sio_module._mcp_tools_call_sync(server, {"name": "echo", "arguments": {"text": "visible"}}, server_name="ok") == "visible"

    assert "echo server log: visible" in capfd.readouterr().err


@pytest.fixture
def listening_url():
    """A socket that accepts connections and never answers, so a request fails only on what the client sends."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    listener.close()


@pytest.mark.parametrize("transport", ["http", "sse"])
@pytest.mark.parametrize("token", ["Bearer sk-SECRET123\n", "Bearer sk-SECRET123 "])
def test_a_header_value_http_refuses_is_a_config_error_that_never_carries_the_token(protocol_2_platform, listening_url, transport, token):
    server = {"type": transport, "url": f"{listening_url}/mcp", "headers": {"Authorization": token}, "startup_timeout": 3}
    load = asyncio.run(load_servers({"remote": server}))[0]
    registry = registry_with(healthy=["ok"])
    registry.record_load(load)
    link = connect(protocol_2_platform, registry, {"remote": server, "ok": fixtures.missing_command()})
    live_registry = registry_with(healthy=["remote"])
    live_link = connect(protocol_2_platform, live_registry, {"remote": server})

    failed_reply = protocol_2_platform.call_tool("remote")
    live_reply = live_link.on_mcp_tools_call({"server": "remote", "params": {"name": "echo", "arguments": {}}})

    assert load.failure.kind == INVALID_CONFIG
    assert "SECRET123" not in protocol_2_platform.payloads[0]["failed_servers"][0]["message"]
    assert "SECRET123" not in failed_reply["content"][0]["text"]
    assert "SECRET123" not in live_reply["content"][0]["text"]
    assert 'a name or value in "headers" is not valid in HTTP' in live_reply["content"][0]["text"]
    live_link.sio.reconnection = False
    protocol_2_platform.close(link)


def test_the_platform_waits_for_a_slow_start_plus_a_slow_call_that_fit_the_reported_budget(protocol_2_platform, tmp_path):
    server = fixtures.starts_and_works_slowly(tmp_path, start_seconds=2, work_seconds=2)
    config = {**protocol_2_platform.config({"slow": server}), "timeout": 3, "startup_timeout": 5}
    link = start_socket_connection(config, registry_with(healthy=["slow"]))
    reported = protocol_2_platform.payloads[0]["toolkit_configs"][0]["timeout_tools_call"]

    started = time.monotonic()
    result = protocol_2_platform.server.call("mcp_tools_call", {"server": "slow", "params": {"name": "work", "arguments": {}}},
                                             to=protocol_2_platform.sids[-1], timeout=reported)

    assert reported == 5 + 3 + 5
    assert time.monotonic() - started > config["timeout"]
    assert result == "done"
    protocol_2_platform.close(link)


def test_each_server_reports_its_own_call_budget_and_an_old_platform_gets_the_largest(legacy_platform):
    servers = {"quick": {"command": "x"}, "npx_based": {"command": "x", "startup_timeout": 120}, "kept": {"command": "x", "stateful": True}}
    config = {**legacy_platform.config(servers), "timeout": 30, "startup_timeout": 30}
    link = start_socket_connection(config, registry_with(healthy=list(servers)))

    payload = legacy_platform.payloads[0]
    budgets = {toolkit["name"]: toolkit["timeout_tools_call"] for toolkit in payload["toolkit_configs"]}

    assert budgets == {"quick": 30 + 30 + 5, "npx_based": 120 + 30 + 5, "kept": 2 * (30 + 5) + 30}
    assert payload["timeout_tools_call"] == 155
    assert payload["timeout_tools_list"] == 30
    link.disconnect()


@pytest.mark.parametrize("server", [{"command": "x", "startup_timeout": 50}, {"command": "x", "stateful": True, "startup_timeout": 50}])
def test_the_reported_call_budget_is_the_one_the_client_enforces(legacy_platform, server):
    config = {**legacy_platform.config({"s": server}), "timeout": 20, "startup_timeout": 30}
    link = start_socket_connection(config, registry_with(healthy=["s"]))
    reported = legacy_platform.payloads[0]["toolkit_configs"][0]["timeout_tools_call"]
    manager = sio_module.get_session_manager()

    with patch.object(manager, "_run_in_loop", return_value="ok") as run, patch.object(manager, "_sessions", {}):
        if server.get("stateful"):
            manager.call_tool_with_recovery_sync("s", server, {"name": "t", "arguments": {}})
        else:
            sio_module._mcp_tools_call_sync(server, {"name": "t", "arguments": {}}, server_name="s",
                                            startup_timeout=read_startup_timeout(server, link.startup_timeout))

    assert run.call_args.kwargs["timeout"] == reported
    run.call_args.args[0].close()
    link.disconnect()


def test_a_tool_error_quoting_a_bracketed_url_is_still_answered_with_a_named_error(protocol_2_platform):
    link = connect(protocol_2_platform, registry_with(healthy=["api"]), {"api": {"command": "x"}})

    with patch.object(sio_module, "_mcp_tools_call_sync", side_effect=RuntimeError("upstream [http://api.internal:8080] returned 502")):
        result = link.on_mcp_tools_call({"server": "api", "params": {"name": "get", "arguments": {}}})

    assert result["isError"] is True
    assert result["content"][0]["text"].startswith('Server "api": tool "get" failed: upstream [http://api.internal')
    link.disconnect()
