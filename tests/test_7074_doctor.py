"""`elitea-mcp doctor` and tray Help → Run Diagnostics (#7074)."""
import json
import os
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.elitea_mcp import main as main_module
from src.elitea_mcp import tray as tray_module
from src.elitea_mcp.tray_platforms import base as tray_platform_base
from src.elitea_mcp.utils import client_identity, diagnostics, preflight, session_status
from src.elitea_mcp.utils.diagnostics import (CheckResult, DoctorContext, Status, exit_code, fail, format_report,
                                              ok, run_checks, warn)
from src.elitea_mcp.utils.package_updates import UpdateState
from src.elitea_mcp.utils.server_loader import (AUTH, COMMAND_NOT_FOUND, TIMEOUT, ServerFailure, ServerLoad,
                                                identify_failure)
from src.elitea_mcp.utils.server_registry import ServerEntry, ServerRegistry
from src.elitea_mcp.utils.server_supervisor import ServerSupervisor
from tests import fixtures_7076 as servers

ORIGIN = "https://elitea.example.com"
TOKEN = "platform-token-7074"
ENV_SECRET = "env-secret-7074"
HEADER_SECRET = "header-secret-7074"
URL_SECRET = "url-secret-7074"
SECRETS = (TOKEN, ENV_SECRET, HEADER_SECRET, URL_SECRET)


def write_config(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


def platform_config(**overrides):
    return {"deployment_url": ORIGIN, "auth_token": TOKEN, "project_id": 13, "servers": {}, **overrides}


def answer(status_code=200, content_type="application/json"):
    response = MagicMock(status_code=status_code, is_redirect=False)
    response.headers = {"content-type": content_type}
    return response


def healthy_platform(url, **kwargs):
    if "/socket.io/" in url:
        return answer(content_type="text/plain")
    return answer()


@pytest.fixture
def no_pypi():
    with patch.object(diagnostics, "fetch_update_state", return_value=UpdateState(current="0.1.36", latest="0.1.36")) as fetch:
        yield fetch


def run_doctor(tmp_path, config, platform_answer=healthy_platform):
    context = DoctorContext(write_config(tmp_path, config), tmp_path / "logs")
    with patch.object(preflight.requests, "get", side_effect=platform_answer):
        return run_checks(context)


def find(results, title_start):
    return [result for result in results if result.title.startswith(title_start)]


# The runner and the report

def test_groups_run_at_the_same_time_but_report_in_the_declared_order(tmp_path):
    started = []

    def slow_first(context):
        started.append("first")
        time.sleep(0.3)
        return [ok("first")]

    def fast_second(context):
        started.append("second")
        return [ok("second")]

    groups = (("First", slow_first), ("Second", fast_second))
    began = time.monotonic()
    with patch.object(diagnostics, "CHECK_GROUPS", groups):
        results = run_checks(DoctorContext(tmp_path / "missing.json", tmp_path))

    assert [result.title for result in results] == ["first", "second"]
    assert time.monotonic() - began < 0.6
    assert sorted(started) == ["first", "second"]


def test_a_check_that_crashes_is_a_failure_not_a_pass(tmp_path):
    def crash(context):
        raise KeyError("boom")

    with patch.object(diagnostics, "CHECK_GROUPS", (("Platform", crash), ("Client", lambda context: [ok("fine")]))):
        results = run_checks(DoctorContext(tmp_path / "missing.json", tmp_path))

    assert results[0] == fail("Platform checks", "the check crashed (KeyError)")
    assert results[1] == ok("fine")
    assert exit_code(results) == 1


def test_only_a_failure_fails_the_exit_code():
    assert exit_code([ok("a"), warn("update available")]) == 0
    assert exit_code([ok("a"), fail("token")]) == 1


def test_the_report_has_one_line_per_check_a_hint_under_problems_and_a_summary():
    results = [ok("Config JSON valid"), warn("SSL verification", "off", "Turn it on."),
               fail("Token", "invalid", "Generate a new one."), ok("Port 8000 free", "", "never shown")]

    assert format_report(results) == (
        "ELITEA MCP doctor\n"
        "  ✓ Config JSON valid\n"
        "  ⚠ SSL verification: off\n"
        "      Turn it on.\n"
        "  ✗ Token: invalid\n"
        "      Generate a new one.\n"
        "  ✓ Port 8000 free\n"
        "\n"
        "1 problem, 1 warning.\n")


def test_a_clean_report_says_all_checks_passed():
    assert format_report([ok("a")]).endswith("\nAll checks passed.\n")


# Config

def test_a_missing_config_fails_every_check_that_needs_it(tmp_path, no_pypi):
    results = run_checks(DoctorContext(tmp_path / "config.json", tmp_path))

    assert find(results, "Config file")[0].status is Status.FAILED
    assert find(results, "Platform") == [fail("Platform", "no usable config to check")]
    assert find(results, "Local servers") == [fail("Local servers", "no usable config to check")]
    assert exit_code(results) == 1


def test_broken_json_is_reported_without_the_secret_on_its_line(tmp_path, no_pypi):
    path = tmp_path / "config.json"
    path.write_text('{"auth_token": "' + TOKEN + '" "deployment_url": "x"}')

    results = run_checks(DoctorContext(path, tmp_path))

    assert find(results, "Config JSON")[0].status is Status.FAILED
    assert TOKEN not in format_report(results)


def test_schema_errors_and_warnings_get_their_own_lines(tmp_path, no_pypi):
    config = platform_config(servers={"github": {"command": "npx", "args": "not-a-list"}}, unknown_key=1)

    results = run_doctor(tmp_path, config)

    schema = find(results, "Config schema")
    assert {result.status for result in schema} == {Status.FAILED, Status.WARNING}
    assert any("servers.github.args" in result.detail for result in schema)
    assert find(results, 'Local server "github"')[0].detail.startswith("skipped: ")


# Client

def test_an_available_update_is_a_warning_with_the_upgrade_command(tmp_path):
    state = UpdateState(current="0.1.36", latest="0.1.38")
    with patch.object(diagnostics, "fetch_update_state", return_value=state):
        result = diagnostics.check_version(DoctorContext(tmp_path, tmp_path, raw={}))

    assert result.status is Status.WARNING
    assert result.detail == "latest: 0.1.38 — update available"
    assert "Update from the tray" in result.hint


@pytest.mark.parametrize("value", [False, 0, None])
def test_update_check_off_sends_no_request_to_pypi(tmp_path, value):
    config = platform_config(update_check=value)
    context = DoctorContext(tmp_path, tmp_path, raw=config, report=diagnostics.check_config(config))
    with patch.object(diagnostics, "fetch_update_state") as fetch:
        result = diagnostics.check_version(context)

    fetch.assert_not_called()
    assert result.status is Status.OK


@pytest.mark.parametrize("value", [False, 0, None])
def test_update_check_off_is_honoured_even_when_the_config_has_errors(tmp_path, value):
    with patch.object(diagnostics, "fetch_update_state") as fetch:
        diagnostics.check_version(DoctorContext(tmp_path, tmp_path, raw={"update_check": value, "servers": []}))

    fetch.assert_not_called()


# Platform

def test_every_platform_step_reports_even_after_the_platform_is_unreachable(tmp_path, no_pypi):
    def unreachable(url, **kwargs):
        raise requests.exceptions.ConnectionError("refused")

    results = run_doctor(tmp_path, platform_config(), unreachable)

    platform_steps = [(result.title, result.status) for result in results
                      if result.title in ("Deployment URL", "Project ID", "Platform reachable", "Token", "Project access")]
    assert platform_steps == [("Deployment URL", Status.OK), ("Project ID", Status.OK), ("Platform reachable", Status.FAILED),
                              ("Token", Status.FAILED), ("Project access", Status.FAILED)]
    assert find(results, "Token")[0].detail == "Cannot check the token until the platform is reachable."


@pytest.mark.parametrize("socket_answer", [answer(404, "text/html"), answer(200, "text/html")])
def test_the_token_is_never_sent_to_an_address_that_is_not_elitea(tmp_path, no_pypi, socket_answer):
    requested = []

    def not_elitea(url, **kwargs):
        requested.append((url, kwargs.get("headers")))
        return socket_answer

    results = run_doctor(tmp_path, platform_config(), not_elitea)

    assert [url for url, _ in requested] == [f"{ORIGIN}/socket.io/?EIO=4&transport=polling"]
    assert requested[0][1] is None
    assert find(results, "Token")[0].status is Status.FAILED


def test_a_rejected_token_leaves_project_access_unchecked_instead_of_asking_without_it(tmp_path, no_pypi):
    def rejecting(url, **kwargs):
        if "/auth/user/" in url:
            return answer(401)
        return healthy_platform(url, **kwargs)

    results = run_doctor(tmp_path, platform_config(), rejecting)

    assert find(results, "Token")[0].status is Status.FAILED
    assert find(results, "Project access") == [fail("Project access", "Cannot check project access until the token check passes.")]


def test_a_healthy_platform_passes_every_step_and_names_the_project(tmp_path, no_pypi):
    results = run_doctor(tmp_path, platform_config(ssl_verify=True))

    assert find(results, "Deployment URL") == [ok("Deployment URL", ORIGIN)]
    assert find(results, "Token") == [ok("Token", "accepted")]
    assert find(results, "Project access") == [ok("Project access", "project 13")]
    assert find(results, "Platform reachable")[0].detail.startswith(f"{ORIGIN}, ")
    assert find(results, "SSL verification") == []


def test_ssl_verification_off_is_a_warning(tmp_path, no_pypi):
    results = run_doctor(tmp_path, platform_config())

    assert find(results, "SSL verification")[0].status is Status.WARNING


def test_a_url_with_a_page_path_is_a_warning_naming_the_base_address(tmp_path, no_pypi):
    results = run_doctor(tmp_path, platform_config(deployment_url=f"{ORIGIN}/app/13/mcp"))

    assert find(results, "Deployment URL")[0].status is Status.WARNING
    assert (f"  ⚠ Deployment URL: has a page or endpoint path; connecting to the base address {ORIGIN}\n"
            '      Set "deployment_url" to the base address.\n') in format_report(results)


def test_the_pre_flight_still_stops_at_the_first_failure_without_further_requests():
    get = MagicMock(side_effect=requests.exceptions.ConnectionError("refused"))
    with patch.object(preflight.requests, "get", get), pytest.raises(preflight.PreflightError, match="Cannot reach"):
        preflight.validate_platform_config(platform_config(), lambda message: None)

    assert get.call_count == 1


def test_a_missing_token_is_reported_even_when_the_platform_is_unreachable(tmp_path, no_pypi):
    def unreachable(url, **kwargs):
        raise requests.exceptions.ConnectionError("refused")

    results = run_doctor(tmp_path, platform_config(auth_token=""), unreachable)

    assert find(results, "Token")[0].detail.startswith('"auth_token" is not set.')


def test_stopping_during_a_slow_start_frees_the_session_at_once(tmp_path):
    from src.elitea_mcp.utils import server_controller
    loading = threading.Event()
    finish_loading = threading.Event()
    supervisor = ServerSupervisor({"servers": {"jira": {}}, "timeout": 30})

    def slow_load(names=None):
        loading.set()
        finish_loading.wait(10)
        return []

    controller = server_controller.ServerController()
    with patch.object(supervisor, "load", side_effect=slow_load):
        run = threading.Thread(target=supervisor.start, daemon=True)
        run.start()
        assert loading.wait(5)
        controller.is_running, controller.daemon_mode, controller.supervisor, controller.server_thread = True, False, supervisor, run
        lock_path = session_status.get_session_lock_path()
        assert client_identity.is_lock_held_elsewhere(lock_path) is True

        with patch.object(server_controller, "MONITOR_DELAY", 0.1):
            controller.stop_servers()

        assert client_identity.is_lock_held_elsewhere(lock_path) is False
        finish_loading.set()
        run.join(5)


def test_the_pre_flight_sends_no_request_once_an_earlier_step_failed():
    get = MagicMock(side_effect=AssertionError("no request expected"))
    with patch.object(preflight.requests, "get", get), pytest.raises(preflight.PreflightError, match="must be a number"):
        preflight.validate_platform_config(platform_config(project_id="abc"), lambda message: None)

    get.assert_not_called()


# Local servers

def test_each_server_gets_a_line_with_its_tools_or_its_reason(tmp_path, no_pypi):
    config = platform_config(servers={"healthy": servers.healthy(tmp_path), "missing": servers.missing_command()})

    results = run_doctor(tmp_path, config)

    assert find(results, 'Local server "healthy"') == [ok('Local server "healthy" (stdio)', "started, 4 tools")]
    missing = find(results, 'Local server "missing"')[0]
    assert (missing.status, missing.detail) == (Status.FAILED, 'command "elitea-test-command-that-does-not-exist" not found')
    assert "Details:" not in missing.hint
    assert not (tmp_path / "logs" / "missing-error.txt").exists()


def test_a_server_with_no_tools_is_a_warning(tmp_path):
    context = DoctorContext(tmp_path, tmp_path, report=MagicMock(config=platform_config()))

    result = diagnostics.describe_load(context, ServerLoad("empty", "stdio", tools=[]))

    assert result == warn('Local server "empty" (stdio)', "started, 0 tools")


def test_no_configured_server_is_a_warning(tmp_path, no_pypi):
    results = run_doctor(tmp_path, platform_config())

    assert find(results, "Local servers")[0].status is Status.WARNING


def test_secrets_never_reach_the_report_and_the_server_output_goes_to_its_details_file(tmp_path, no_pypi):
    echoing = servers.exits_at_start(f"boom {TOKEN} {ENV_SECRET}")
    echoing["env"] = {"API_KEY": ENV_SECRET}
    config = platform_config(servers={
        "echoing": echoing,
        "remote": {"type": "http", "url": f"http://127.0.0.1:{servers.find_closed_port()}/mcp?key={URL_SECRET}",
                   "headers": {"Authorization": f"Bearer {HEADER_SECRET}"}},
    })

    results = run_doctor(tmp_path, config)

    report = format_report(results)
    assert not [secret for secret in SECRETS if secret in report]
    assert "boom" not in report
    details = tmp_path / "logs" / "echoing-error.txt"
    assert f"Details: {details}" in find(results, 'Local server "echoing"')[0].hint
    text = details.read_text()
    assert "boom **** ****" in text
    assert not [secret for secret in SECRETS if secret in text]


def test_a_launcher_timeout_says_the_first_run_may_still_be_downloading():
    npx = {"type": "stdio", "command": "/usr/local/bin/npx", "args": ["-y", "server"]}
    pnpm = {"type": "stdio", "command": "pnpm", "args": ["dlx", "server"]}
    plain = {"type": "stdio", "command": "python", "args": ["server.py"]}

    assert "first run of npx may still be downloading" in identify_failure(TimeoutError(), npx, 30).hint
    assert "first run of pnpm dlx" in identify_failure(TimeoutError(), pnpm, 30).hint
    assert identify_failure(TimeoutError(), plain, 30).hint == 'Check the command/URL or increase "startup_timeout".'


# Environment: port and running client

def test_a_port_in_use_is_a_warning_that_names_the_sse_mode(tmp_path):
    import socket
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        context = DoctorContext(tmp_path, tmp_path, report=MagicMock(config=platform_config(host="127.0.0.1", port=port)))

        assert diagnostics.check_port(context) == warn(f"Port {port} in use",
                                                       "only matters for elitea-mcp run --transport sse")


@pytest.mark.parametrize("port", [70000, -1, 0, "abc"])
def test_a_port_that_cannot_be_listened_on_is_a_warning_not_a_crash(tmp_path, port):
    context = DoctorContext(tmp_path, tmp_path, report=MagicMock(config=platform_config(port=port)))

    results = diagnostics.check_environment(context)

    assert results[0] == warn(f'Port "{port}"', "not a port number from 1 to 65535",
                              "Only matters for elitea-mcp run --transport sse.")
    assert results[1].title == "Running client"
    assert exit_code(results) == 0


def test_an_unknown_host_is_not_reported_as_a_busy_port(tmp_path):
    context = DoctorContext(tmp_path, tmp_path, report=MagicMock(config=platform_config(host="no-such-host.invalid")))

    assert diagnostics.check_port(context).title == 'Host "no-such-host.invalid"'


SESSION_HOLDER = ("import sys, time\n"
                  "from pathlib import Path\n"
                  "from src.elitea_mcp.utils import client_identity, session_status\n"
                  "client_identity.get_config_dir = lambda: Path(sys.argv[1])\n"
                  "held = client_identity.take_lock(session_status.get_session_lock_path())\n"
                  "print(held is not None, flush=True)\n"
                  "time.sleep(60)\n")


@pytest.fixture
def other_session(tmp_path):
    """Another process running servers with this config, the way a `serve` holds the session lock."""
    holder = subprocess.Popen([sys.executable, "-c", SESSION_HOLDER, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "True"
    yield holder
    holder.kill()
    holder.wait()


def test_the_session_lock_tells_a_running_session_apart_from_none(tmp_path, other_session):
    lock_path = session_status.get_session_lock_path()
    assert client_identity.is_lock_held_elsewhere(lock_path) is True
    other_session.kill()
    other_session.wait()
    assert client_identity.is_lock_held_elsewhere(lock_path) is False


def test_no_running_client_is_reported_as_none(tmp_path):
    assert diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path)) == [ok("Running client", "none")]


def write_status(tmp_path, entries, age=0, connected=True):
    status = {"pid": 1, "updated_at": time.time() - age, "connected": connected,
              "servers": [session_status.describe_entry(entry) for entry in entries]}
    (tmp_path / session_status.STATUS_FILE_NAME).write_text(json.dumps(status))


def build_session():
    registry = ServerRegistry()
    registry.record_load(ServerLoad("jira", "stdio", tools=[{"name": "search"}, {"name": "create"}]))
    registry.record_load(ServerLoad("github", "stdio", tools=[{"name": "pr"}]))
    registry.record_load(ServerLoad("figma", "sse", failure=ServerFailure(AUTH, "401 Unauthorized", 'Check the token in "headers".')))
    registry.mark_all_registered(["jira"])
    registry.apply_registration(["github"], set(), [{"name": "github", "reason": "name_already_registered"}])
    return registry.list_entries()


def test_another_clients_live_session_is_read_from_its_status_file(tmp_path, other_session):
    write_status(tmp_path, build_session())

    results = diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path))

    assert results[0].status is Status.WARNING and "another client is running servers" in results[0].detail
    assert [(result.status, result.title, result.detail) for result in results[1:]] == [
        (Status.OK, "Running session", "connected to the platform"),
        (Status.OK, 'Running session: "jira" (stdio)', "2 tools, registered"),
        (Status.WARNING, 'Running session: "github" (stdio)', "declined (name already registered)"),
        (Status.FAILED, 'Running session: "figma" (sse)', "failed: 401 Unauthorized"),
    ]
    assert "rename the server" in results[3].hint


def test_a_serve_beside_an_idle_tray_is_still_seen(tmp_path, other_session):
    client_identity.hold_lock(tmp_path / client_identity.CLIENT_ID_LOCK_NAME)
    write_status(tmp_path, build_session())

    results = diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path))

    assert "another client is running servers" in results[0].detail
    assert len(results) == 5


def test_a_frozen_client_is_not_trusted(tmp_path, other_session):
    write_status(tmp_path, build_session(), age=session_status.STALE_AFTER_SECONDS + 5)

    results = diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path))

    assert [result.title for result in results] == ["Running client", "Running session"]
    assert "may be frozen" in results[1].detail


def test_a_running_client_without_a_status_file_is_named_without_rows(tmp_path, other_session):
    results = diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path))

    assert results[1].detail == "it has not published its server status yet"


def test_a_status_file_without_its_lock_holder_is_ignored(tmp_path):
    write_status(tmp_path, build_session())

    assert diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path)) == [ok("Running client", "none")]


def test_the_tray_shows_its_own_session_from_memory(tmp_path):
    results = diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path, build_session))

    assert "this tray" in results[0].detail
    assert [result.title for result in results[1:]] == [
        'Running session: "jira" (stdio)', 'Running session: "github" (stdio)', 'Running session: "figma" (sse)']


# The running session publishes its status

def test_only_the_session_lock_holder_publishes_and_stopping_hands_the_session_over(tmp_path):
    entries = build_session()
    first = session_status.SessionStatusPublisher(lambda: entries, lambda: True)
    second = session_status.SessionStatusPublisher(lambda: entries[:1], lambda: False)
    status_file = tmp_path / session_status.STATUS_FILE_NAME

    first.publish()
    second.publish()
    written = json.loads(status_file.read_text())
    assert (written["connected"], [item["name"] for item in written["servers"]]) == (True, ["jira", "github", "figma"])
    assert status_file.stat().st_mode & 0o077 == 0

    first.stop()
    first.publish()
    assert not status_file.exists()
    assert not client_identity.is_lock_held_elsewhere(session_status.get_session_lock_path())

    second.publish()
    assert [item["name"] for item in json.loads(status_file.read_text())["servers"]] == ["jira"]
    second.stop()


def test_the_status_says_connected_as_soon_as_the_platform_link_is_up(tmp_path):
    from src.elitea_mcp.utils import server_supervisor
    supervisor = ServerSupervisor({"servers": {"jira": {}}, "timeout": 30})

    def load(names=None):
        supervisor.registry.record_load(ServerLoad("jira", "stdio", tools=[{"name": "search"}]))
        return []

    with patch.object(supervisor, "load", side_effect=load), \
            patch.object(server_supervisor, "start_socket_connection", return_value=MagicMock(connected=True)):
        assert supervisor.start() is True

    assert json.loads((tmp_path / session_status.STATUS_FILE_NAME).read_text())["connected"] is True
    supervisor.stop()


def test_the_status_file_stays_next_to_the_lock_it_was_taken_with(tmp_path):
    publisher = session_status.SessionStatusPublisher(lambda: [], lambda: True)
    publisher.publish()
    elsewhere = tmp_path / "another-config-dir"
    elsewhere.mkdir()

    with patch.object(client_identity, "get_config_dir", return_value=elsewhere):
        publisher.publish()
        assert not (elsewhere / session_status.STATUS_FILE_NAME).exists()
        publisher.stop()

    assert not (tmp_path / session_status.STATUS_FILE_NAME).exists()


def test_a_run_cancelled_before_it_loads_never_takes_the_session(tmp_path):
    cancelled = threading.Event()
    cancelled.set()
    supervisor = ServerSupervisor({"servers": {"jira": {}}, "timeout": 30}, cancelled=cancelled)

    with patch.object(supervisor, "load") as load:
        assert supervisor.start() is False

    load.assert_not_called()
    assert not (tmp_path / session_status.STATUS_FILE_NAME).exists()
    assert not client_identity.is_lock_held_elsewhere(session_status.get_session_lock_path())


def test_stop_from_the_tray_menu_frees_the_session_without_waiting_for_the_platform(tmp_path):
    from src.elitea_mcp.utils import server_controller
    supervisor = ServerSupervisor({"servers": {"jira": {}}, "timeout": 30})
    supervisor.session_status.publish()
    supervisor.link = MagicMock(disconnect=MagicMock(side_effect=lambda: time.sleep(3)))
    controller = server_controller.ServerController()
    controller.is_running, controller.daemon_mode, controller.supervisor = True, False, supervisor

    began = time.monotonic()
    controller.stop_servers()

    assert time.monotonic() - began < 1
    supervisor.link.disconnect.assert_not_called()
    assert not client_identity.is_lock_held_elsewhere(session_status.get_session_lock_path())


def test_a_session_this_process_runs_is_never_reported_as_another_client(tmp_path):
    publisher = session_status.SessionStatusPublisher(build_session, lambda: True)
    publisher.publish()

    assert session_status.read_running_session() is None
    assert diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path)) == [ok("Running client", "none")]
    publisher.stop()


def test_a_tray_still_checking_the_platform_does_not_claim_a_second_copy_of_its_servers(tmp_path):
    results = diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path, lambda: []))

    assert results == [ok("Running client", "this tray, its servers not started yet")]


def test_the_tray_names_another_client_that_already_runs_servers_with_its_config(tmp_path, other_session):
    write_status(tmp_path, build_session())

    results = diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path, build_session))

    assert results[1] == warn("Running client", "another client is also running servers with this config",
                              "Stop one of them: the second copy's servers are declined by the platform.")
    assert [result.title for result in results[2:]] == [
        'Running session: "jira" (stdio)', 'Running session: "github" (stdio)', 'Running session: "figma" (sse)']


def test_a_publish_waiting_for_the_write_lock_writes_the_newest_servers(tmp_path):
    entries = [ServerEntry("jira", "stdio")]
    publisher = session_status.SessionStatusPublisher(lambda: list(entries), lambda: False)
    publisher.publish()

    with publisher._write_lock:
        waiting = threading.Thread(target=publisher.publish)
        waiting.start()
        time.sleep(0.2)
        entries[0] = ServerEntry("jira", "stdio", status="healthy", tools=[{"name": "search"}])
    waiting.join(5)

    assert json.loads((tmp_path / session_status.STATUS_FILE_NAME).read_text())["servers"][0]["status"] == "healthy"
    publisher.stop()


def test_a_server_round_trips_through_the_status_file():
    for entry in build_session():
        restored = session_status.restore_entry(json.loads(json.dumps(session_status.describe_entry(entry))))
        assert (restored.mark, restored.describe_status()) == (entry.mark, entry.describe_status())


def test_the_supervisor_publishes_from_the_start_of_loading_and_every_change_until_stop(tmp_path):
    supervisor = ServerSupervisor({"servers": {"jira": {}}, "timeout": 30})
    status_file = tmp_path / session_status.STATUS_FILE_NAME
    seen_while_loading = []

    def load(names=None):
        seen_while_loading.append(json.loads(status_file.read_text())["servers"][0]["status"])
        supervisor.registry.record_load(ServerLoad("jira", "stdio", failure=ServerFailure(COMMAND_NOT_FOUND, "missing", "")))
        return []

    with patch.object(supervisor, "load", side_effect=load):
        assert supervisor.start() is False

    assert seen_while_loading == ["pending"]
    assert json.loads(status_file.read_text())["servers"][0]["failure"]["reason"] == "missing"
    supervisor.stop()
    assert not status_file.exists()


# CLI

def test_doctor_prints_the_report_and_exits_with_its_code(tmp_path, capsys):
    with patch.object(main_module, "run_checks", side_effect=lambda context, on_results: on_results([fail("Token", "bad")]) or [fail("Token", "bad")]), \
            patch.object(main_module, "get_default_paths", return_value={"tray_log": tmp_path / "tray.log"}), \
            pytest.raises(SystemExit) as exited:
        main_module.doctor(str(tmp_path / "config.json"))

    assert exited.value.code == 1
    assert capsys.readouterr().out == "ELITEA MCP doctor\n  ✗ Token: bad\n\n1 problem.\n"


def test_help_lists_doctor_and_version_prints_the_version(capsys):
    with patch.object(sys, "argv", ["elitea-mcp", "--help"]), pytest.raises(SystemExit):
        main_module.run_cli()
    assert "doctor" in capsys.readouterr().out

    with patch.object(sys, "argv", ["elitea-mcp", "--version"]), pytest.raises(SystemExit) as exited:
        main_module.run_cli()
    assert exited.value.code == 0
    assert capsys.readouterr().out.startswith("elitea-mcp ")


def test_validate_points_to_doctor_when_the_config_has_errors(tmp_path, capsys):
    path = write_config(tmp_path, {"servers": []})
    with patch.object(main_module, "write_config_schema"), pytest.raises(SystemExit):
        main_module.validate(str(path))

    assert "Run elitea-mcp doctor for a full check." in capsys.readouterr().err


# Tray

@pytest.fixture
def app(tmp_path):
    controller = MagicMock(daemon_mode=False, is_running=True)
    controller.list_servers.return_value = build_session()
    controller.supervisor.cancelled = controller.stop_event
    with patch.object(tray_module, "get_server_controller", return_value=controller), \
            patch.object(tray_module, "UpdateChecker"), patch.object(tray_module.MCPTrayApp, "_read_config", return_value=None):
        app = tray_module.MCPTrayApp()
    app.platform.open_file = MagicMock()
    app.platform.copy_to_clipboard = MagicMock(return_value=True)
    app.show_notification = MagicMock()
    app.show_error = MagicMock()
    return app


def find_help_item(app):
    help_menu = next(item for item in app.get_menu_items() if item.text == "Help").submenu
    return help_menu.items[0]


def test_run_diagnostics_saves_opens_and_copies_the_report_off_the_menu_thread(app, tmp_path):
    results = [ok("Config JSON valid"), fail("Token", "invalid")]
    finished = threading.Event()
    app.update_menu = MagicMock(side_effect=lambda: finished.set() if not app.diagnostics_running else None)
    with patch.object(tray_module, "run_checks", return_value=results) as run, \
            patch.object(tray_module, "get_default_paths", return_value={"tray_log": tmp_path / "tray.log"}):
        find_help_item(app)(MagicMock())
        assert finished.wait(5)

    report_path = tmp_path / diagnostics.REPORT_FILE_NAME
    assert report_path.read_text() == format_report(results)
    app.platform.open_file.assert_called_once_with(report_path)
    app.platform.copy_to_clipboard.assert_called_once_with(format_report(results))
    app.show_notification.assert_called_with("Diagnostics", "1 problem. Report opened and copied to the clipboard.")
    assert [entry.name for entry in run.call_args.args[0].list_local_session()] == ["jira", "github", "figma"]


def test_the_menu_item_is_disabled_while_diagnostics_run(app):
    app.diagnostics_running = True

    item = find_help_item(app)

    assert (item.text, item.enabled) == ("Running Diagnostics…", False)


def test_a_daemon_session_is_not_read_from_the_tray_memory(app):
    app.server_controller.daemon_mode = True

    assert app.list_local_session() is None


def test_a_restart_still_checking_the_platform_never_shows_the_last_runs_servers(app):
    app.server_controller.stop_event = threading.Event()

    assert app.list_local_session() == []


def test_a_failed_start_points_to_run_diagnostics(app):
    app.server_controller.start_servers.return_value = False
    app.is_server_running = False
    with patch.object(app, "_read_config", return_value={"deployment_url": ORIGIN, "auth_token": TOKEN}):
        app.start_server()

    assert "Help → Run Diagnostics" in app.show_error.call_args.args[1]


def test_a_platform_error_on_the_tray_points_to_run_diagnostics(app):
    app.update_menu = MagicMock()

    app.on_server_error("Authentication failed: the token is invalid or expired.")

    assert app.show_error.call_args.args[1].endswith("Help → Run Diagnostics checks the whole setup.")


def test_serve_points_to_doctor_when_the_platform_refuses_the_settings(capsys):
    from src.elitea_mcp.commands import run_servers as run_servers_module
    report = MagicMock(notable=[], config=platform_config())
    with patch.object(run_servers_module, "load_config", return_value={}), \
            patch.object(run_servers_module, "check_config", return_value=report), \
            patch.object(run_servers_module, "write_config_schema"), \
            patch.object(run_servers_module, "validate_platform_config", side_effect=preflight.PreflightError("bad token")), \
            pytest.raises(SystemExit):
        run_servers_module.run_servers()

    assert capsys.readouterr().out == "Cannot connect to platform: bad token\nRun elitea-mcp doctor for a full check.\n"


def test_a_clipboard_tool_that_cannot_take_the_marks_still_opens_the_report(app, tmp_path):
    app.platform.copy_to_clipboard = type(app.platform).copy_to_clipboard.__get__(app.platform)
    encode_error = UnicodeEncodeError("charmap", "✓", 0, 1, "character maps to <undefined>")
    with patch.object(tray_module, "run_checks", return_value=[ok("Config JSON valid")]), \
            patch.object(tray_module, "get_default_paths", return_value={"tray_log": tmp_path / "tray.log"}), \
            patch.object(tray_platform_base.subprocess, "run", side_effect=encode_error):
        app.write_diagnostics_report()

    app.platform.open_file.assert_called_once_with(tmp_path / diagnostics.REPORT_FILE_NAME)
    app.show_error.assert_not_called()
    assert app.show_notification.call_args.args[1].startswith("All checks passed. Report opened: ")


def test_a_long_error_notification_is_cut_to_what_windows_accepts(app):
    app.show_error = tray_module.MCPTrayApp.show_error.__get__(app)
    app.update_menu = MagicMock()
    shown = []

    def notify(text, title):
        if len(text) > 256:
            raise ValueError("string too long")
        shown.append(text)

    app.icon = MagicMock(notify=notify)

    app.on_server_error("x" * 230)

    assert len(shown[0]) <= 250 and shown[0].endswith("...")


def test_doctor_writes_utf8_when_its_output_is_redirected_on_a_legacy_code_page(tmp_path):
    import io
    redirected = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    results = [ok("Config JSON valid"), warn("SSL verification", "off"), fail("Token", "bad")]
    with patch.object(main_module, "run_checks", side_effect=lambda context, on_results: on_results(results) or results), \
            patch.object(main_module, "get_default_paths", return_value={"tray_log": tmp_path / "tray.log"}), \
            patch.object(sys, "stdout", redirected), pytest.raises(SystemExit) as exited:
        main_module.doctor(str(tmp_path / "config.json"))

    redirected.flush()
    assert exited.value.code == 1
    assert redirected.buffer.getvalue().decode("utf-8").endswith("✗ Token: bad\n\n1 problem, 1 warning.\n")


def test_a_secret_in_a_server_url_path_never_reaches_the_report(tmp_path, no_pypi):
    path_secret = "path-secret-7074"
    config = platform_config(servers={"hosted": {
        "type": "http", "url": f"http://127.0.0.1:{servers.find_closed_port()}/api/mcp/s/{path_secret}/mcp", "headers": {}}})

    results = run_doctor(tmp_path, config)

    assert path_secret in find(results, 'Local server "hosted"')[0].detail
    report = format_report(results)
    assert path_secret not in report
    assert 'Local server "hosted" (http): cannot reach http://127.0.0.1:' in report


def test_a_running_sessions_url_reason_is_cut_to_scheme_and_host_in_the_report():
    results = diagnostics.describe_session([ServerEntry(
        "hosted", "http", status="failed",
        failure=ServerFailure(TIMEOUT, "cannot reach https://mcp.example.com/s/path-secret-7074/mcp: timed out", ""))])

    assert diagnostics.format_results(results) == '  ✗ Running session: "hosted" (http): failed: cannot reach https://mcp.example.com: timed out'


RUNS_AS_ROOT_OR_ON_WINDOWS = os.name == "nt" or os.geteuid() == 0
PERMISSION_BITS_IGNORED = pytest.mark.skipif(RUNS_AS_ROOT_OR_ON_WINDOWS,
                                             reason="Windows and root ignore POSIX permission bits")


@pytest.mark.parametrize("make_unreadable", [
    "directory",
    pytest.param("no_permission", marks=PERMISSION_BITS_IGNORED),
    pytest.param("unsearchable_folder", marks=PERMISSION_BITS_IGNORED),
])
def test_a_config_that_cannot_be_read_fails_its_line_and_every_group_still_runs(tmp_path, no_pypi, make_unreadable):
    locked = None
    if make_unreadable == "directory":
        config_path = tmp_path / "config-dir"
        config_path.mkdir()
    elif make_unreadable == "no_permission":
        config_path = locked = write_config(tmp_path, platform_config())
    else:
        folder = tmp_path / "config-folder"
        folder.mkdir()
        config_path = write_config(folder, platform_config())
        locked = folder
    if locked is not None:
        locked.chmod(0o600 if locked.is_dir() else 0)

    try:
        results = run_checks(DoctorContext(config_path, tmp_path))
    finally:
        if locked is not None:
            locked.chmod(0o700)

    config_line = find(results, "Config file")[0]
    assert config_line.status is Status.FAILED and config_line.detail.startswith(f"cannot read {config_path}: ")
    assert find(results, "Platform") == [fail("Platform", "no usable config to check")]
    assert find(results, "Running client")
