import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.elitea_mcp.commands import run_servers as run_servers_module
from src.elitea_mcp.config_model import check_config
from src.elitea_mcp.utils import server_supervisor as supervisor_module
from src.elitea_mcp.utils.server_loader import AUTH, COMMAND_NOT_FOUND, EXITED, TIMEOUT, ServerFailure, ServerLoad
from src.elitea_mcp.utils import server_registry as registry_module
from src.elitea_mcp.utils.server_registry import FAILED, HEALTHY, ServerRegistry
from src.elitea_mcp.utils.server_supervisor import ServerSupervisor, collect_skipped_servers
from tests import fixtures_7076 as fixtures

PLATFORM = {"deployment_url": "http://127.0.0.1:9", "auth_token": "platform-token-123", "project_id": 13,
            "timeout": 30, "startup_timeout": 3}
WAIT_SECONDS = 10


def wait_until(condition):
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.05)
    assert condition()


def fake_platform():
    """Stand in for start_socket_connection: register every healthy server and hand back a link whose sync is recorded."""
    link = MagicMock()

    def connect(config, registry, *args):
        registry.mark_all_registered([entry.name for entry in registry.list_by_status(HEALTHY)])
        link.registry = registry
        return link

    return patch.object(supervisor_module, "start_socket_connection", side_effect=connect), link


def supervisor_stopped_by_ctrl_c(*args, **kwargs):
    """A real supervisor whose session wait in serve is interrupted as by Ctrl+C."""
    supervisor = ServerSupervisor(*args, **kwargs)
    supervisor.cancelled = MagicMock(wait=MagicMock(side_effect=KeyboardInterrupt), is_set=MagicMock(return_value=False))
    return supervisor


def failure_load(name, kind, reason="it broke"):
    return ServerLoad(name, "stdio", failure=ServerFailure(kind, reason, "Fix it."))


def scripted_loads(*rounds):
    """Answer each load with the next round of results, keeping only the servers asked for, and record the names."""
    calls = []
    rounds = iter(rounds)

    async def load(servers, startup_timeout, list_timeout):
        calls.append(sorted(servers))
        by_name = {result.name: result for result in next(rounds)}
        return [by_name[name] for name in servers]

    return patch.object(supervisor_module, "load_servers", side_effect=load), calls


def test_when_every_server_fails_the_client_never_connects_and_shows_the_summary(tmp_path):
    notify_error = MagicMock()
    supervisor = ServerSupervisor({**PLATFORM, "servers": {"github": fixtures.missing_command(), "jira": fixtures.exits_at_start()}},
                                  notify_error=notify_error)
    platform, _ = fake_platform()

    with platform as connect:
        assert supervisor.start() is False

    connect.assert_not_called()
    notify_error.assert_called_once_with(
        '0 of 2 MCP servers connected. Failed: github (command "elitea-test-command-that-does-not-exist" not found), '
        'jira (exited during start).')


def test_after_start_each_failure_and_then_the_summary_are_announced(tmp_path):
    notify_server = MagicMock()
    supervisor = ServerSupervisor({**PLATFORM, "servers": {"ok": fixtures.healthy(tmp_path), "github": fixtures.missing_command()}},
                                  notify_server=notify_server, summary_suffix="See Servers menu.")
    platform, _ = fake_platform()

    with platform:
        assert supervisor.start() is True

    assert [c.args[0] for c in notify_server.call_args_list] == [
        'Server "github": command "elitea-test-command-that-does-not-exist" not found. Install it or use the full path in "command".',
        '1 of 2 MCP servers connected. Failed: github (command "elitea-test-command-that-does-not-exist" not found). See Servers menu.',
    ]
    assert supervisor.registry.get("ok").format_menu_row() == "✓ ok — 4 tools, registered"
    supervisor.stop()


def test_retry_reloads_only_the_failed_servers_and_registers_the_recovered_one():
    servers = {"ok": {}, "jira": {}, "github": {}}
    loads, calls = scripted_loads(
        [ServerLoad("ok", "stdio", tools=[]), failure_load("jira", EXITED), failure_load("github", COMMAND_NOT_FOUND)],
        [ServerLoad("jira", "stdio", tools=[{"name": "t"}]), failure_load("github", COMMAND_NOT_FOUND)],
    )
    platform, link = fake_platform()
    supervisor = ServerSupervisor({**PLATFORM, "servers": servers})

    with loads, platform:
        supervisor.start()
        supervisor.retry_failed()

    assert calls == [["github", "jira", "ok"], ["github", "jira"]]
    link.sync.assert_called_once_with(["jira"])
    assert supervisor.registry.get("jira").status == HEALTHY
    assert supervisor.registry.get("github").status == FAILED
    supervisor.stop()


@patch.object(supervisor_module, "RETRY_DELAYS", (0.05, 0.05))
def test_a_timed_out_server_keeps_being_retried_at_the_last_delay_after_the_schedule():
    notify_server = MagicMock()
    loads, calls = scripted_loads(*[[ServerLoad("ok", "stdio", tools=[]), failure_load("figma", TIMEOUT, "did not respond")]] * 6)
    platform, _ = fake_platform()
    supervisor = ServerSupervisor({**PLATFORM, "servers": {"ok": {}, "figma": {}}}, notify_server=notify_server)

    with loads, platform:
        supervisor.start()
        wait_until(lambda: len(calls) >= 5)
        wait_until(lambda: supervisor.registry.get("figma").format_menu_row()
                   == "✗ figma — failed: did not respond (retrying automatically)")
        supervisor.stop()

    assert calls[:5] == [["figma", "ok"], ["figma"], ["figma"], ["figma"], ["figma"]]
    assert [c.args[0] for c in notify_server.call_args_list].count(
        'Server "figma": did not respond. Fix it. Retrying automatically every 0.05 s.') == 1


@pytest.mark.parametrize("attempts, jitter, retry_after, expected", [
    (0, 0.0, None, 15.0),
    (0, 15.0, None, 30.0),
    (9, 0.0, None, 150.0),
    (9, 150.0, None, 300.0),
    (0, 0.0, 120.0, 120.0),
    (9, 150.0, 10.0, 300.0),
])
def test_each_wait_is_jittered_within_its_step_and_never_shorter_than_retry_after(attempts, jitter, retry_after, expected):
    entry = MagicMock(retry_attempts=attempts, failure=ServerFailure(TIMEOUT, "slow", retry_after=retry_after))

    with patch.object(supervisor_module.random, "uniform", return_value=jitter) as uniform:
        assert supervisor_module.calculate_retry_delay(entry) == expected

    step = supervisor_module.RETRY_DELAYS[min(attempts, len(supervisor_module.RETRY_DELAYS) - 1)]
    uniform.assert_called_once_with(0, step / 2)


def test_a_server_that_breaks_again_soon_after_recovering_keeps_its_long_wait():
    registry = ServerRegistry()
    registry.record_load(failure_load("api", TIMEOUT))
    for _ in range(4):
        registry.record_retry_scheduled("api")
    failure = ServerFailure(TIMEOUT, "slow")

    with patch.object(registry_module.time, "monotonic", return_value=1000.0):
        registry.record_load(ServerLoad("api", "http", tools=[]))
    with patch.object(registry_module.time, "monotonic", return_value=1000.0 + registry_module.HEALTHY_PERIOD_THAT_RESETS_RETRIES - 1):
        registry.record_transport_failure("api", failure)
        registry.record_transport_failure("api", failure)
    assert registry.get("api").retry_attempts == 4

    with patch.object(registry_module.time, "monotonic", return_value=5000.0):
        registry.record_load(ServerLoad("api", "http", tools=[]))
    with patch.object(registry_module.time, "monotonic", return_value=5000.0 + registry_module.HEALTHY_PERIOD_THAT_RESETS_RETRIES):
        registry.record_transport_failure("api", failure)
        registry.record_transport_failure("api", failure)
    assert registry.get("api").retry_attempts == 0


def test_a_failure_that_cannot_fix_itself_shows_no_automatic_retry():
    registry = ServerRegistry()
    registry.record_load(failure_load("github", COMMAND_NOT_FOUND, 'command "npx" not found'))

    assert registry.get("github").format_menu_row() == '✗ github — failed: command "npx" not found'


@patch.object(supervisor_module, "RETRY_DELAYS", (0.05,))
def test_a_recovered_server_is_announced_and_registered_by_the_automatic_retry():
    notify_server = MagicMock()
    loads, _ = scripted_loads([failure_load("figma", TIMEOUT), ServerLoad("ok", "stdio", tools=[])],
                              [ServerLoad("figma", "stdio", tools=[{"name": "t"}])])
    platform, link = fake_platform()
    supervisor = ServerSupervisor({**PLATFORM, "servers": {"ok": {}, "figma": {}}}, notify_server=notify_server)

    with loads, platform:
        supervisor.start()
        wait_until(lambda: supervisor.registry.get("figma").status == HEALTHY)

    link.sync.assert_called_once_with(["figma"])
    notify_server.assert_called_with('Server "figma" recovered: 1 tools.')
    supervisor.stop()


@pytest.mark.parametrize("kind", [COMMAND_NOT_FOUND, AUTH, EXITED])
def test_failures_that_cannot_fix_themselves_are_never_retried_automatically(kind):
    loads, calls = scripted_loads([ServerLoad("ok", "stdio", tools=[]), failure_load("broken", kind)])
    platform, _ = fake_platform()
    supervisor = ServerSupervisor({**PLATFORM, "servers": {"ok": {}, "broken": {}}})

    with loads, platform, patch.object(supervisor_module, "RETRY_DELAYS", (0.01,)):
        supervisor.start()
        time.sleep(0.2)

    assert calls == [["broken", "ok"]]
    assert supervisor._timers == {}
    supervisor.stop()


@patch.object(supervisor_module, "RETRY_DELAYS", (0.05,))
def test_a_registered_server_that_breaks_mid_session_is_reported_and_retried():
    notify_server = MagicMock()
    loads, calls = scripted_loads([ServerLoad("ok", "stdio", tools=[])], [ServerLoad("ok", "stdio", tools=[])])
    platform, _ = fake_platform()
    supervisor = ServerSupervisor({**PLATFORM, "servers": {"ok": {}}}, notify_server=notify_server)

    with loads, platform:
        supervisor.start()
        failure = ServerFailure(EXITED, "exited during start", "Check it.")
        supervisor.registry.record_transport_failure("ok", failure)
        assert supervisor.registry.record_transport_failure("ok", failure) is True
        supervisor.handle_server_failure("ok")
        wait_until(lambda: supervisor.registry.get("ok").status == HEALTHY)

    assert calls == [["ok"], ["ok"]]
    notify_server.assert_any_call('Server "ok": exited during start. Check it.')
    supervisor.stop()


@pytest.mark.parametrize("kind", [COMMAND_NOT_FOUND, AUTH])
def test_a_registered_server_that_breaks_with_a_permanent_failure_is_not_retried(kind):
    loads, calls = scripted_loads([ServerLoad("ok", "stdio", tools=[])])
    platform, _ = fake_platform()
    supervisor = ServerSupervisor({**PLATFORM, "servers": {"ok": {}}})

    with loads, platform, patch.object(supervisor_module, "RETRY_DELAYS", (0.01,)):
        supervisor.start()
        failure = ServerFailure(kind, "it broke", "Fix it.")
        supervisor.registry.record_transport_failure("ok", failure)
        supervisor.registry.record_transport_failure("ok", failure)
        supervisor.handle_server_failure("ok")
        time.sleep(0.2)

    assert calls == [["ok"]]
    assert supervisor._timers == {}
    supervisor.stop()


@patch.object(supervisor_module, "RETRY_DELAYS", (30,))
def test_stopping_cancels_the_scheduled_retries():
    loads, _ = scripted_loads([ServerLoad("ok", "stdio", tools=[]), failure_load("figma", TIMEOUT)])
    platform, link = fake_platform()
    supervisor = ServerSupervisor({**PLATFORM, "servers": {"ok": {}, "figma": {}}})

    with loads, platform:
        supervisor.start()
        timer = supervisor._timers["figma"]
        supervisor.stop()

    assert timer.finished.is_set()
    link.disconnect.assert_called_once()


def test_a_server_the_config_check_skipped_is_listed_as_failed_with_its_reason():
    raw = {**PLATFORM, "servers": {"ok": {"command": "npx"}, "playwright": {"type": "stdi0", "command": "npx"}}}
    report = check_config(raw)

    supervisor = ServerSupervisor(report.config, collect_skipped_servers(report, raw))

    entry = supervisor.registry.get("playwright")
    assert entry.status == FAILED
    assert entry.message == ('Server "playwright": invalid configuration: type: "stdi0" is not a server type; use "stdio", '
                             '"http" or "sse". Fix it in config.json and restart.')
    assert not entry.can_auto_retry


@patch.object(run_servers_module, "write_config_schema")
@patch.object(run_servers_module, "validate_platform_config", side_effect=lambda config: config)
def test_serve_prints_a_table_and_the_masked_last_lines_of_a_failed_server(mock_validate, mock_write_schema, tmp_path, capsys):
    stderr = "\n".join(f"line {number}" for number in range(1, 7)) + "\nusing ghp_secret now"
    servers = {"ok": fixtures.healthy(tmp_path), "jira": {**fixtures.exits_at_start(stderr), "env": {"GITHUB_TOKEN": "ghp_secret"}}}
    platform, _ = fake_platform()

    with patch.object(run_servers_module, "load_config", return_value={**PLATFORM, "servers": servers}), platform, \
            patch.object(run_servers_module, "ServerSupervisor", supervisor_stopped_by_ctrl_c), pytest.raises(SystemExit):
        run_servers_module.run_servers()

    out = capsys.readouterr().out
    assert "MCP servers:\n  ✓ ok    stdio  4 tools, registered\n  ✗ jira  stdio  failed: exited during start\n" in out
    assert "Last output of jira:\n    line 3\n    line 4\n    line 5\n    line 6\n    using **** now\n" in out


@patch.object(run_servers_module, "write_config_schema")
@patch.object(run_servers_module, "validate_platform_config", side_effect=lambda config: config)
def test_serve_exits_without_connecting_when_every_server_fails(mock_validate, mock_write_schema, capsys):
    platform, _ = fake_platform()

    with patch.object(run_servers_module, "load_config", return_value={**PLATFORM, "servers": {"github": fixtures.missing_command()}}), \
            platform as connect, pytest.raises(SystemExit) as exit_info:
        run_servers_module.run_servers()

    assert exit_info.value.code == 1
    connect.assert_not_called()
    assert "  ✗ github  stdio  failed: command" in capsys.readouterr().out


def test_the_error_report_holds_reason_hint_and_masked_output_but_the_failure_message_does_not():
    supervisor = ServerSupervisor({**PLATFORM, "servers": {"jira": {"env": {"TOKEN": "s3cr3t-value"}}}})
    supervisor.registry.record_load(ServerLoad("jira", "stdio", failure=ServerFailure(EXITED, "exited during start", "Check it."),
                                               stderr_tail=["auth s3cr3t-value failed"]))

    assert supervisor.format_error_report("jira") == (
        'Server "jira" (stdio)\nStatus: failed: exited during start\nReason: exited during start\nHint: Check it.\n'
        "\nLast output:\n    auth **** failed\n")
    assert "s3cr3t" not in supervisor.registry.get("jira").message


def test_a_run_stopped_while_its_servers_load_never_connects():
    cancelled = threading.Event()

    async def load_then_get_stopped(servers, startup_timeout, list_timeout):
        cancelled.set()
        return [ServerLoad(name, "stdio", tools=[]) for name in servers]

    platform, _ = fake_platform()
    supervisor = ServerSupervisor({**PLATFORM, "servers": {"ok": {}}}, cancelled=cancelled)

    with patch.object(supervisor_module, "load_servers", side_effect=load_then_get_stopped), platform as connect:
        assert supervisor.start() is False

    connect.assert_not_called()


def test_a_retry_that_finishes_before_the_platform_answers_still_registers_the_server():
    loads, _ = scripted_loads([ServerLoad("good", "stdio", tools=[]), failure_load("flaky", EXITED)],
                              [ServerLoad("flaky", "stdio", tools=[])])
    platform_answered = threading.Event()
    link = MagicMock()

    def answer_slowly(config, registry, *args):
        platform_answered.wait(5)
        registry.mark_all_registered(["good"])
        return link

    supervisor = ServerSupervisor({**PLATFORM, "servers": {"good": {}, "flaky": {}}})
    with loads, patch.object(supervisor_module, "start_socket_connection", side_effect=answer_slowly):
        starter = threading.Thread(target=supervisor.start)
        starter.start()
        wait_until(lambda: supervisor.registry.get("flaky").status == FAILED)
        retry = threading.Thread(target=supervisor.retry_failed)
        retry.start()
        time.sleep(0.2)
        platform_answered.set()
        starter.join(5)
        retry.join(5)

    link.sync.assert_called_once_with(["flaky"])
    supervisor.stop()


def test_no_row_claims_a_retry_when_every_server_failed_at_start_and_none_is_scheduled():
    loads, _ = scripted_loads([failure_load("a", TIMEOUT, "did not respond within 30 s")])
    platform, _ = fake_platform()
    supervisor = ServerSupervisor({**PLATFORM, "servers": {"a": {}}})

    with loads, platform:
        assert supervisor.start() is False

    assert supervisor._timers == {}
    assert supervisor.registry.get("a").format_menu_row() == "✗ a — failed: did not respond within 30 s"


@patch.object(supervisor_module, "RETRY_DELAYS", (30,))
def test_a_row_says_retrying_only_while_a_retry_is_pending():
    loads, _ = scripted_loads([ServerLoad("ok", "stdio", tools=[]), failure_load("figma", TIMEOUT, "slow")])
    platform, _ = fake_platform()
    supervisor = ServerSupervisor({**PLATFORM, "servers": {"ok": {}, "figma": {}}})

    with loads, platform:
        supervisor.start()
        pending = supervisor.registry.get("figma").format_menu_row()
        supervisor.stop()

    assert pending == "✗ figma — failed: slow (retrying automatically)"
    assert supervisor.registry.get("figma").format_menu_row() == "✗ figma — failed: slow"
