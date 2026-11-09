import asyncio
import json
import sys
from unittest.mock import MagicMock, patch

import elitea_mcp.config
import pytest
import socketio

from src.elitea_mcp import config as config_module
from src.elitea_mcp import config_model
from src.elitea_mcp import main as main_module
from src.elitea_mcp import tray as tray_module
from src.elitea_mcp.commands import bootstrap_servers as bootstrap_servers_module
from src.elitea_mcp.config import SCHEMA_REFERENCE, interactive_bootstrap, load_config, save_config, set_bootstrap_config
from src.elitea_mcp.config_errors import parse_config
from src.elitea_mcp.config_model import Severity, check_config
from src.elitea_mcp.utils import server_controller as server_controller_module
from src.elitea_mcp.utils import sio as sio_module
from src.elitea_mcp.utils.session_manager import SessionManager, get_session_manager

GOOD = {"command": "npx"}
PLATFORM = {"deployment_url": "https://elitea.example.com", "auth_token": "token", "project_id": 2}


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    """Point the client, and the schema it writes, at a config file in a temporary folder."""
    path = tmp_path / "config.json"
    for module in (config_module, elitea_mcp.config, tray_module, main_module, bootstrap_servers_module):
        monkeypatch.setattr(module, "get_config_file", lambda: path)
    return path


def check(raw):
    return check_config(json.loads(json.dumps(raw)))


def found(report, severity, path, text):
    return any(problem.severity is severity and problem.path == path and text in problem.message
               for problem in report.problems)


@pytest.mark.parametrize("raw, timeout", [
    ({"timeout": "30"}, 30),
    ({"timeout": 2.9}, 2),
    ({"timeout": True}, 1),
    ({}, 30),
])
def test_values_that_work_today_keep_working_without_a_problem(raw, timeout):
    report = check({**raw, "project_id": " 12 ", "port": "8080", "servers": {"a": GOOD}})
    assert report.config["timeout"] == timeout
    assert report.config["project_id"] == 12
    assert report.notable == []


@pytest.mark.parametrize("value", ["abc", None, "30s", "2.9"])
def test_a_timeout_that_is_not_a_number_falls_back_to_30_with_a_warning(value):
    report = check({"timeout": value, "servers": {"a": GOOD}})
    assert report.config["timeout"] == 30
    assert found(report, Severity.WARNING, "timeout", "30 is used")


@pytest.mark.parametrize("value", [0, -5, "-1"])
def test_a_timeout_that_would_end_every_call_at_once_stops_the_client(value):
    report = check({"timeout": value, "servers": {"a": GOOD}})
    assert report.config is None
    assert found(report, Severity.ERROR, "timeout", "must be more than 0 seconds")


@pytest.mark.parametrize("value", ["false", "False", " no ", "off", "0"])
def test_a_boolean_written_as_text_keeps_todays_reading_and_says_how_to_turn_it_off(value):
    report = check({"ssl_verify": value, "update_check": value, "servers": {"a": {**GOOD, "stateful": value}}})
    assert (report.config["ssl_verify"], report.config["update_check"], report.config["servers"]["a"]["stateful"]) == (True, True, True)
    for path in ("ssl_verify", "update_check", "servers.a.stateful"):
        assert found(report, Severity.WARNING, path, "Write false without quotes")


@pytest.mark.parametrize("value", ["true", "yes", 1, True, False, None])
def test_a_boolean_whose_text_means_what_it_is_read_as_raises_nothing(value):
    report = check({"ssl_verify": value, "servers": {"a": GOOD}})
    assert report.config["ssl_verify"] is bool(value)
    assert report.notable == []


@pytest.mark.parametrize("servers", [[], "npx", None, 3])
def test_servers_that_are_not_an_object_stop_the_client_with_a_readable_error(servers):
    report = check({"servers": servers})
    assert report.config is None
    assert found(report, Severity.ERROR, "servers", "must be an object of named servers")


def test_a_broken_server_is_skipped_and_the_others_still_run():
    report = check({"servers": {"good1": GOOD, "remote": {"url": "https://x/mcp"}, "good2": {"command": "uvx", "args": ["srv"]}}})
    assert sorted(report.config["servers"]) == ["good1", "good2"]
    assert report.skipped_servers == ["remote"]
    assert found(report, Severity.ERROR, "servers.remote", 'has a "url" but no "type"; add "type": "http" (or "sse")')


@pytest.mark.parametrize("server, path, text", [
    ({"command": "npx", "args": "--port 3000"}, "servers.s.args", 'must be a list of strings, for example ["--port", "3000"]'),
    ({"comand": "npx"}, "servers.s.command", "is required"),
    ({"type": "websocket", "url": "ws://x"}, "servers.s.type", '"websocket" is not a server type'),
    ({"type": 1, "url": "https://x"}, "servers.s.type", '"1" is not a server type'),
    ({"type": "http", "url": "ws://x"}, "servers.s.url", "must start with http:// or https://"),
    ({"type": "http"}, "servers.s.url", "is required"),
    ({"command": "npx", "env": {"PORT": 3000}}, "servers.s.env.PORT", "must be text in double quotes"),
    ({"type": "sse", "url": "https://x/sse", "headers": ["Bearer x"]}, "servers.s.headers", "must be an object"),
    ("npx", "servers.s", "must be an object such as"),
])
def test_a_server_config_that_cannot_work_is_skipped_with_what_to_fix(server, path, text):
    report = check({"servers": {"s": server, "ok": GOOD}})
    assert list(report.config["servers"]) == ["ok"]
    assert found(report, Severity.ERROR, path, text)
    assert "The server is skipped." in next(problem.message for problem in report.errors if problem.path == path)


@pytest.mark.parametrize("server_type", ["streamableHttp", "streamable-http", "streamable_http", "HTTP", "http"])
def test_streamable_http_spellings_of_other_clients_run_as_http_and_headers_are_optional(server_type):
    report = check({"servers": {"r": {"type": server_type, "url": "https://x/mcp"}}})
    assert report.config["servers"]["r"]["type"] == "http"
    assert report.config["servers"]["r"]["headers"] == {}
    assert report.notable == []


@pytest.mark.parametrize("server_type", ["http", "sse"])
def test_null_headers_keep_working_as_no_headers(server_type):
    report = check({"servers": {"r": {"type": server_type, "url": "https://x/mcp", "headers": None}}})
    assert report.config["servers"]["r"]["headers"] == {}
    assert report.notable == []


def test_a_url_next_to_a_command_keeps_running_as_stdio_as_it_does_today():
    report = check({"servers": {"a": {"command": "npx", "url": "https://x"}}})
    assert report.config["servers"]["a"]["type"] == "stdio"
    assert found(report, Severity.WARNING, "servers.a.url", "is not a setting this client reads")


@pytest.mark.parametrize("raw, path, suggestion", [
    ({"Servers": {}}, "Servers", "servers"),
    ({"projectId": 2}, "projectId", "project_id"),
    ({"deploymentUrl": "x"}, "deploymentUrl", "deployment_url"),
    ({"timout": 5}, "timout", "timeout"),
    ({"servers": {"a": {"command": "npx", "comand": "x"}}}, "servers.a.comand", "command"),
    ({"servers": {"a": {"command": "npx", "agrs": []}}}, "servers.a.agrs", "args"),
    ({"servers": {"a": {"command": "npx", "ENV": {}}}}, "servers.a.ENV", "env"),
    ({"servers": {"r": {"type": "http", "url": "https://x", "header": {}}}}, "servers.r.header", "headers"),
])
def test_a_key_the_client_does_not_read_is_ignored_with_a_typo_hint(raw, path, suggestion):
    report = check(raw)
    assert found(report, Severity.WARNING, path, f'is not a setting this client reads, so it is ignored. Did you mean "{suggestion}"?')


@pytest.mark.parametrize("key, value", [("autoApprove", []), ("disabled", True), ("envFile", ".env"), ("timeout", 60)])
def test_another_clients_server_key_is_reported_as_ignored_without_a_wrong_suggestion(key, value):
    report = check({"servers": {"a": {**GOOD, key: value}}})
    assert found(report, Severity.WARNING, f"servers.a.{key}", "is not a setting this client reads, so it is ignored.")
    assert not found(report, Severity.WARNING, f"servers.a.{key}", "Did you mean")
    assert list(report.config["servers"]) == ["a"]


def test_mcp_servers_is_read_when_servers_is_missing():
    report = check({"mcpServers": {"github": GOOD}})
    assert list(report.config["servers"]) == ["github"]
    assert found(report, Severity.WARNING, "mcpServers", 'is read as "servers"; rename it to "servers"')
    assert not found(report, Severity.WARNING, "mcpServers", "is not a setting")


def test_mcp_servers_is_ignored_when_servers_is_set_so_nothing_new_starts_after_an_update():
    report = check({"servers": {"a": GOOD}, "mcpServers": {"b": GOOD}})
    assert list(report.config["servers"]) == ["a"]
    assert found(report, Severity.WARNING, "mcpServers", 'is ignored because "servers" is set')


def test_names_that_clean_to_the_same_name_keep_todays_winner_and_name_both():
    report = check({"servers": {"my-server": {"command": "a"}, "my_server": {"command": "b"}}})
    assert report.config["servers"] == {"my_server": report.config["servers"]["my_server"]}
    assert report.config["servers"]["my_server"]["command"] == "b"
    assert found(report, Severity.WARNING, "servers.my_server", '"my-server" and "my_server" all become "my_server"; only "my_server" runs')


def test_a_clash_whose_last_entry_is_broken_names_the_one_that_still_runs():
    report = check({"servers": {"my-server": {"command": "a"}, "my_server": {"command": 5}}})
    assert report.config["servers"]["my_server"]["command"] == "a"
    assert found(report, Severity.WARNING, "servers.my_server", 'only "my-server" runs')


def test_a_name_without_a_letter_is_skipped_and_a_plain_rename_is_only_an_info():
    report = check({"servers": {"123": GOOD, "brave-search": GOOD}})
    assert list(report.config["servers"]) == ["brave_search"]
    assert found(report, Severity.ERROR, "servers.123", "the name must contain a letter")
    assert found(report, Severity.INFO, "servers.brave-search", 'is registered on the platform as "brave_search"')
    assert not found(report, Severity.WARNING, "servers.brave-search", "")


def test_a_key_written_twice_is_reported_and_the_last_value_is_used(tmp_path):
    raw = parse_config('{"servers": {"a": {"command": "x"}, "a": {"command": "y", "command": "z"}}, "timeout": 5, "timeout": 6}',
                       tmp_path / "config.json")
    report = check_config(raw)
    assert report.config["servers"]["a"]["command"] == "z"
    assert report.config["timeout"] == 6
    for path in ("servers.a", "servers.a.command", "timeout"):
        assert found(report, Severity.WARNING, path, "appears more than once; only the last one is used")


@pytest.mark.parametrize("value", [123, ["https://x"], {"url": "https://x"}, True])
def test_a_deployment_url_that_is_not_text_is_reported_instead_of_crashing_the_check(value):
    report = check({"deployment_url": value, "servers": {"a": GOOD}})
    assert report.config is None
    assert found(report, Severity.ERROR, "deployment_url", "must be text in double quotes")


def test_a_blocked_config_names_the_blocker_before_the_skipped_servers():
    report = check({"project_id": "abc", "servers": {"remote": {"url": "https://x/mcp"}}})
    assert report.summary.startswith('project_id: must be a number, got "abc"')


@pytest.mark.parametrize("raw, path, text", [
    ({"deployment_url": "dev.elitea.ai"}, "deployment_url", "must be the address of ELITEA"),
    ({"project_id": "abc"}, "project_id", 'must be a number, got "abc"'),
])
def test_connection_settings_that_fail_today_stop_the_client(raw, path, text):
    report = check({**raw, "servers": {"a": GOOD}})
    assert report.config is None
    assert found(report, Severity.ERROR, path, text)


def test_a_deployment_url_with_a_page_path_is_left_for_the_preflight_notice():
    report = check({"deployment_url": "https://dev.elitea.ai/app/2/mcp", "servers": {"a": GOOD}})
    assert report.config["deployment_url"] == "https://dev.elitea.ai/app/2/mcp"


def test_the_schema_reference_is_a_known_key_and_its_absence_is_only_an_info():
    assert check({"$schema": SCHEMA_REFERENCE}).problems == []
    assert found(check({}), Severity.INFO, "$schema", SCHEMA_REFERENCE)


def test_every_notification_summary_fits_a_windows_balloon():
    long_name = "x" * 400
    report = check({"servers": {long_name: {"url": "https://x"}, "b": {"url": "https://y"}}})
    assert len(report.summary) <= 256
    assert len(report.summarize_skipped_servers(2)) <= 256
    assert report.summary.endswith("(+1 more, see Validate Config)")


def test_bootstrap_keeps_what_the_user_wrote(config_file):
    original = {"timeout": "abc", "autoApprove": [], "servers": {"my-server": {"command": "a"}, "my_server": {"command": "b"}}}
    config_file.write_text(json.dumps(original))
    set_bootstrap_config("https://elitea.example.com", "new-token")
    saved = json.loads(config_file.read_text())
    assert saved == {**original, "deployment_url": "https://elitea.example.com", "auth_token": "new-token",
                     "host": "0.0.0.0", "port": 8000}


def test_bootstrap_starts_a_new_file_with_the_schema_reference(config_file):
    set_bootstrap_config("https://elitea.example.com", "token")
    assert next(iter(json.loads(config_file.read_text()))) == "$schema"


def test_enter_at_the_project_prompt_skips_and_leaves_the_servers_alone(config_file, monkeypatch):
    save_config({"project_id": 2, "servers": {"a": GOOD, "b": GOOD}})
    before = config_file.read_text()
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=[""]))
    bootstrap_servers_module.bootstrap_servers({})
    assert config_file.read_text() == before


def test_bootstrap_adds_a_server_without_removing_the_existing_ones(config_file, monkeypatch):
    save_config({"project_id": 2, "servers": {"a": GOOD, "b": GOOD}})
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=["3", "c", "2", "uvx", "", "n", ""]))
    bootstrap_servers_module.bootstrap_servers({})
    saved = json.loads(config_file.read_text())
    assert saved["project_id"] == 3
    assert list(saved["servers"]) == ["a", "b", "c"]


def test_bootstrap_moves_servers_read_from_mcp_servers_so_they_keep_running(config_file, monkeypatch):
    save_config({"project_id": 2, "mcpServers": {"a": GOOD}})
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=["3", "c", "2", "uvx", "", "n", ""]))
    bootstrap_servers_module.bootstrap_servers({})
    saved = json.loads(config_file.read_text())
    assert "mcpServers" not in saved
    assert list(check_config(saved).config["servers"]) == ["a", "c"]


def test_bootstrap_leaves_an_ignored_mcp_servers_alone_when_servers_is_set(config_file, monkeypatch):
    save_config({"project_id": 2, "servers": {"a": GOOD}, "mcpServers": {"b": GOOD}})
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=["3", "c", "2", "uvx", "", "n", ""]))
    bootstrap_servers_module.bootstrap_servers({})
    saved = json.loads(config_file.read_text())
    assert (list(saved["servers"]), saved["mcpServers"]) == (["a", "c"], {"b": GOOD})


def test_a_port_typed_at_the_prompt_is_saved_as_a_number(config_file, monkeypatch):
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=["https://elitea.example.com", "token", "", "9000"]))
    interactive_bootstrap()
    assert json.loads(config_file.read_text())["port"] == 9000


def test_a_tool_call_uses_the_timeout_of_the_started_config_without_reading_the_file(monkeypatch):
    monkeypatch.setattr(config_module, "load_config", MagicMock(side_effect=AssertionError("read the file")))
    manager = SessionManager()
    manager.timeout = 0.05
    with pytest.raises(TimeoutError):
        manager._run_in_loop(asyncio.sleep(1))


def test_starting_the_socket_hands_its_timeout_to_the_session_manager(monkeypatch):
    client = MagicMock()
    client.connect.side_effect = socketio.exceptions.ConnectionError("refused")
    monkeypatch.setattr(sio_module.socketio, "Client", MagicMock(return_value=client))
    monkeypatch.setattr(sio_module.time, "sleep", lambda seconds: None)
    with pytest.raises(sio_module.PlatformConnectionError):
        sio_module.start_socket_connection({**PLATFORM, "timeout": 7, "ssl_verify": False}, [])
    assert get_session_manager().timeout == 7


def controller_with(config_file, raw):
    config_file.write_text(json.dumps({**PLATFORM, **raw}))
    controller = server_controller_module.ServerController()
    controller._notify_error = MagicMock()
    controller._start_embedded_servers = MagicMock(return_value=True)
    return controller


def test_start_skips_a_broken_server_with_one_message_and_runs_the_rest(config_file, monkeypatch):
    monkeypatch.setattr(server_controller_module, "load_config", load_config)
    controller = controller_with(config_file, {"servers": {"a": GOOD, "b": GOOD, "remote": {"url": "https://x/mcp"}}})
    assert controller.start_servers() is True
    assert sorted(controller.config["servers"]) == ["a", "b"]
    controller._notify_error.assert_called_once()
    assert controller._notify_error.call_args[0][0].startswith('Skipped 1 of 3 servers: servers.remote: has a "url" but no "type"')


def test_start_refuses_a_config_that_cannot_run_and_says_why(config_file, monkeypatch):
    monkeypatch.setattr(server_controller_module, "load_config", load_config)
    controller = controller_with(config_file, {"timeout": -5, "servers": {"a": GOOD}})
    assert controller.start_servers() is False
    controller._start_embedded_servers.assert_not_called()
    assert "timeout: must be more than 0 seconds" in controller._notify_error.call_args[0][0]


def test_start_with_every_server_skipped_says_why_instead_of_asking_for_bootstrap(config_file, monkeypatch):
    monkeypatch.setattr(server_controller_module, "load_config", load_config)
    controller = controller_with(config_file, {"servers": {"remote": {"url": "https://x/mcp"}}})
    assert controller.start_servers() is False
    assert controller._notify_error.call_args[0][0].startswith('Skipped 1 of 1 servers: servers.remote: has a "url" but no "type"')


def test_start_with_every_server_skipped_names_the_server_even_after_a_top_level_warning(config_file, monkeypatch):
    monkeypatch.setattr(server_controller_module, "load_config", load_config)
    controller = controller_with(config_file, {"ssl_verify": "false", "servers": {"github": {"command": "npx", "args": "-y server"}}})
    assert controller.start_servers() is False
    assert controller._notify_error.call_args[0][0].startswith("Skipped 1 of 1 servers: servers.github.args: must be a list")


def test_start_writes_the_schema_next_to_the_config(config_file, monkeypatch):
    monkeypatch.setattr(server_controller_module, "load_config", load_config)
    controller_with(config_file, {"servers": {"a": GOOD}}).start_servers()
    schema = json.loads((config_file.parent / "config.schema.json").read_text())
    assert {"$schema", "servers", "timeout", "ssl_verify"} <= set(schema["properties"])


def build_tray():
    with patch.object(tray_module, "get_server_controller", return_value=MagicMock(daemon_mode=False)), \
            patch.object(tray_module, "UpdateChecker"):
        app = tray_module.MCPTrayApp()
    app.show_notification = MagicMock()
    app.report_update_result = MagicMock()
    return app


def menu_texts(app):
    return [item.text for item in app.get_menu_items()]


def test_the_tray_treats_a_config_that_cannot_run_like_a_broken_file(config_file):
    config_file.write_text(json.dumps({**PLATFORM, "servers": []}))
    app = build_tray()
    texts = menu_texts(app)
    assert "Config File Has Errors" in texts
    assert "Start MCP Server" not in texts
    app._on_tray_ready(MagicMock())
    app.show_notification.assert_any_call("Invalid Config", 'servers: must be an object of named servers, for example {"github": {"command": "npx"}}')


def test_a_config_that_cannot_run_still_honours_its_update_settings(config_file):
    config_file.write_text(json.dumps({**PLATFORM, "timeout": -5, "update_check": False}))
    assert build_tray().update_checker is None
    config_file.write_text(json.dumps({**PLATFORM, "timeout": -5}))
    assert build_tray().update_checker is not None


def test_problems_that_do_not_stop_the_client_show_one_quiet_menu_line_and_no_notification(config_file):
    config_file.write_text(json.dumps({**PLATFORM, "ssl_verify": "false", "servers": {"a": GOOD, "remote": {"url": "https://x"}}}))
    app = build_tray()
    texts = menu_texts(app)
    assert "Config has 2 problems (Validate Config to see)" in texts
    assert "Start MCP Server" in texts
    app._on_tray_ready(MagicMock())
    app.show_notification.assert_not_called()


def test_validate_config_reports_problems_that_do_not_stop_the_client(config_file):
    config_file.write_text(json.dumps({**PLATFORM, "ssl_verify": "false", "servers": {"a": GOOD}}))
    app = build_tray()
    app.validate_config()
    app.show_notification.assert_called_once_with(
        "Config Has Problems", 'ssl_verify: "false" is text, which this client reads as true. Write false without quotes to turn it off.')


def test_open_config_file_creates_a_file_that_references_the_schema(config_file):
    app = build_tray()
    app._open_file = MagicMock()
    app.open_config_file()
    assert json.loads(config_file.read_text()) == {"$schema": SCHEMA_REFERENCE}
    assert (config_file.parent / "config.schema.json").exists()


def run_cli(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["elitea-mcp", *args])
    try:
        main_module.cli()
    except SystemExit as exit_:
        return exit_.code
    return 0


def test_validate_lists_every_problem_and_exits_1_on_an_error(config_file, monkeypatch, capsys):
    config_file.write_text(json.dumps({"ssl_verify": "false", "servers": {"a": GOOD, "remote": {"url": "https://x"}, "b-c": GOOD}}))
    assert run_cli(monkeypatch, "validate") == 1
    err = capsys.readouterr().err
    assert 'error: servers.remote: has a "url" but no "type"' in err
    assert "warning: ssl_verify:" in err
    assert 'info: servers.b-c: is registered on the platform as "b_c"' in err


def test_validate_exits_0_when_there_are_only_warnings(config_file, monkeypatch, capsys):
    config_file.write_text(json.dumps({"$schema": SCHEMA_REFERENCE, "ssl_verify": "false", "servers": {"a": GOOD}}))
    assert run_cli(monkeypatch, "validate") == 0
    out, err = capsys.readouterr()
    assert out == f"Config file is valid: {config_file}\n"
    assert "warning: ssl_verify:" in err


def test_serve_prints_the_problems_and_stops_on_a_config_that_cannot_run(config_file, monkeypatch, capsys):
    config_file.write_text(json.dumps({**PLATFORM, "servers": []}))
    assert run_cli(monkeypatch, "serve") == 1
    assert "error: servers: must be an object of named servers" in capsys.readouterr().err


@pytest.mark.parametrize("value, port", [(8080, 8080), ("8080", 8080), ("abc", None), (0, None), (70000, None), (None, None)])
def test_run_listens_only_on_a_valid_config_port(value, port):
    assert main_module.parse_config_port(value) == port


def run_with_config_port(monkeypatch, transport, port):
    monkeypatch.setattr(main_module, "get_bootstrap_config", lambda: {**PLATFORM, "port": port})
    monkeypatch.setattr(main_module, "validate_platform_config", lambda config, notify: config)
    monkeypatch.setattr(main_module, "Agents", MagicMock())
    monkeypatch.setattr(main_module, "configure_logging", MagicMock())
    run = MagicMock()
    monkeypatch.setattr(main_module, "run", run)
    main_module.main(project_id=2, transport=transport)
    return run


@pytest.mark.parametrize("port", ["800o", None, 0, "", 70000])
def test_run_over_stdio_never_reads_the_config_port_so_a_bad_one_does_not_stop_it(monkeypatch, port):
    assert run_with_config_port(monkeypatch, "stdio", port).called


@pytest.mark.parametrize("port, called_with", [("800o", None), (70000, None), ("8080", 8080)])
def test_run_over_sse_listens_only_on_a_valid_config_port(monkeypatch, port, called_with):
    run = run_with_config_port(monkeypatch, "sse", port)
    assert (run.call_args.kwargs["port"] if run.called else None) == called_with
