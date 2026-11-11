"""`elitea-mcp doctor` and tray Help → Run Diagnostics (#7074)."""
import json
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.elitea_mcp import main as main_module
from src.elitea_mcp import tray as tray_module
from src.elitea_mcp.utils import client_identity, diagnostics, preflight, session_status
from src.elitea_mcp.utils.diagnostics import (CheckResult, DoctorContext, Status, exit_code, fail, format_report,
                                              ok, run_checks, warn)
from src.elitea_mcp.utils.package_updates import UpdateState
from src.elitea_mcp.utils.server_loader import (AUTH, COMMAND_NOT_FOUND, TIMEOUT, ServerFailure, ServerLoad,
                                                identify_failure)
from src.elitea_mcp.utils.server_registry import ServerRegistry
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
    assert "Cannot reach" in find(results, "Token")[0].detail


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
    assert ORIGIN in find(results, "Deployment URL")[0].detail


def test_the_pre_flight_still_stops_at_the_first_failure_without_further_requests():
    get = MagicMock(side_effect=requests.exceptions.ConnectionError("refused"))
    with patch.object(preflight.requests, "get", get), pytest.raises(preflight.PreflightError, match="Cannot reach"):
        preflight.validate_platform_config(platform_config(), lambda message: None)

    assert get.call_count == 1


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


HOLDER = ("import sys, time\n"
          "from src.elitea_mcp.utils import client_identity\n"
          "from pathlib import Path\n"
          "client_identity.get_config_dir = lambda: Path(sys.argv[1])\n"
          "print(client_identity.hold_lock(client_identity.get_client_lock_path()), flush=True)\n"
          "time.sleep(60)\n")


@pytest.fixture
def other_client(tmp_path):
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "True"
    yield holder
    holder.kill()
    holder.wait()


def test_the_lock_tells_another_running_client_apart_from_none_and_from_this_process(tmp_path, other_client):
    assert client_identity.is_client_lock_held_elsewhere() is True
    other_client.kill()
    other_client.wait()
    assert client_identity.is_client_lock_held_elsewhere() is False
    assert client_identity.hold_lock(client_identity.get_client_lock_path()) is True
    assert client_identity.holds_client_lock() is True
    assert client_identity.is_client_lock_held_elsewhere() is False


CONNECTING_CLIENT = ("import sys, time\n"
                     "from src.elitea_mcp.utils import client_identity\n"
                     "from pathlib import Path\n"
                     "client_identity.get_config_dir = lambda: Path(sys.argv[1])\n"
                     "client_identity.load_client_id()\n"
                     "print('connecting', flush=True)\n"
                     "time.sleep(60)\n")


def test_a_new_lock_holder_never_passes_off_the_previous_runs_status_as_its_own(tmp_path):
    write_status(tmp_path, build_session(), age=20)
    connecting = subprocess.Popen([sys.executable, "-c", CONNECTING_CLIENT, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert connecting.stdout.readline().strip() == "connecting"

        session = session_status.read_running_session()

        assert (session.entries, session.problem.startswith("it has not published its server status")) == (None, True)
    finally:
        connecting.kill()
        connecting.wait()


def test_no_running_client_is_reported_as_none(tmp_path):
    assert diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path)) == [ok("Running client", "none")]


def write_status(tmp_path, entries, age=0, running=True, connected=True):
    status = {"pid": 1, "updated_at": time.time() - age, "running": running, "connected": connected,
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


def test_another_clients_live_session_is_read_from_its_status_file(tmp_path, other_client):
    write_status(tmp_path, build_session())

    results = diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path))

    assert results[0].status is Status.WARNING and "another client is connected" in results[0].detail
    assert [(result.status, result.title, result.detail) for result in results[1:]] == [
        (Status.OK, "Running session", "connected to the platform"),
        (Status.OK, 'Running session: "jira" (stdio)', "2 tools, registered"),
        (Status.WARNING, 'Running session: "github" (stdio)', "declined (name already registered)"),
        (Status.FAILED, 'Running session: "figma" (sse)', "failed: 401 Unauthorized"),
    ]
    assert "rename the server" in results[3].hint


def test_a_frozen_client_is_not_trusted(tmp_path, other_client):
    write_status(tmp_path, build_session(), age=session_status.STALE_AFTER_SECONDS + 5)

    results = diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path))

    assert [result.title for result in results] == ["Running client", "Running session"]
    assert "may be frozen" in results[1].detail


def test_a_running_client_without_a_status_file_is_named_without_rows(tmp_path, other_client):
    results = diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path))

    assert "not published its server status" in results[1].detail


def test_a_status_file_without_its_lock_holder_is_ignored(tmp_path):
    write_status(tmp_path, build_session())

    assert diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path)) == [ok("Running client", "none")]


def test_a_client_whose_servers_stopped_is_not_a_warning(tmp_path, other_client):
    write_status(tmp_path, [], age=3600, running=False, connected=False)

    results = diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path))

    assert [result.status for result in results] == [Status.OK]


def test_the_tray_shows_its_own_session_from_memory(tmp_path):
    client_identity.hold_lock(client_identity.get_client_lock_path())

    results = diagnostics.check_running_client(DoctorContext(tmp_path, tmp_path, build_session))

    assert "this tray" in results[0].detail
    assert [result.title for result in results[1:]] == [
        'Running session: "jira" (stdio)', 'Running session: "github" (stdio)', 'Running session: "figma" (sse)']


# The running client publishes its status

def test_the_status_file_is_written_only_by_the_lock_holder_and_ends_as_stopped(tmp_path):
    entries = build_session()
    publisher = session_status.SessionStatusPublisher(lambda: entries, lambda: True)
    status_file = tmp_path / session_status.STATUS_FILE_NAME

    publisher.publish()
    assert not status_file.exists()

    client_identity.hold_lock(client_identity.get_client_lock_path())
    publisher.publish()
    written = json.loads(status_file.read_text())
    assert (written["running"], written["connected"], [item["name"] for item in written["servers"]]) == (
        True, True, ["jira", "github", "figma"])
    assert status_file.stat().st_mode & 0o077 == 0

    publisher.stop()
    publisher.publish()
    assert json.loads(status_file.read_text())["running"] is False


def test_a_server_round_trips_through_the_status_file():
    for entry in build_session():
        restored = session_status.restore_entry(json.loads(json.dumps(session_status.describe_entry(entry))))
        assert (restored.mark, restored.describe_status()) == (entry.mark, entry.describe_status())


def test_the_supervisor_publishes_every_change_once_it_holds_the_lock(tmp_path):
    client_identity.hold_lock(client_identity.get_client_lock_path())
    supervisor = ServerSupervisor({"servers": {"jira": {}}, "timeout": 30})

    supervisor.registry.record_load(ServerLoad("jira", "stdio", failure=ServerFailure(COMMAND_NOT_FOUND, "missing", "")))

    written = json.loads((tmp_path / session_status.STATUS_FILE_NAME).read_text())
    assert written["servers"][0]["failure"]["reason"] == "missing"
    supervisor.stop()
    assert json.loads((tmp_path / session_status.STATUS_FILE_NAME).read_text())["running"] is False


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
    with patch.object(tray_module, "get_server_controller", return_value=controller), \
            patch.object(tray_module, "UpdateChecker"), patch.object(tray_module.MCPTrayApp, "_read_config", return_value=None):
        app = tray_module.MCPTrayApp()
    app._open_file = MagicMock()
    app._copy_to_clipboard = MagicMock(return_value=True)
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
    app._open_file.assert_called_once_with(report_path)
    app._copy_to_clipboard.assert_called_once_with(format_report(results))
    app.show_notification.assert_called_with("Diagnostics", "1 problem. Report opened and copied to the clipboard.")
    assert [entry.name for entry in run.call_args.args[0].list_local_session()] == ["jira", "github", "figma"]


def test_the_menu_item_is_disabled_while_diagnostics_run(app):
    app.diagnostics_running = True

    item = find_help_item(app)

    assert (item.text, item.enabled) == ("Running Diagnostics…", False)


def test_a_daemon_session_is_not_read_from_the_tray_memory(app):
    app.server_controller.daemon_mode = True

    assert app.list_local_session() is None


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
    app._copy_to_clipboard = tray_module.MCPTrayApp._copy_to_clipboard.__get__(app)
    encode_error = UnicodeEncodeError("charmap", "✓", 0, 1, "character maps to <undefined>")
    with patch.object(tray_module, "run_checks", return_value=[ok("Config JSON valid")]), \
            patch.object(tray_module, "get_default_paths", return_value={"tray_log": tmp_path / "tray.log"}), \
            patch.object(tray_module.subprocess, "run", side_effect=encode_error):
        app.write_diagnostics_report()

    app._open_file.assert_called_once_with(tmp_path / diagnostics.REPORT_FILE_NAME)
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
