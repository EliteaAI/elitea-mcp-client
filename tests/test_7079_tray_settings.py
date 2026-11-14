import ast
import builtins
import http.server
import json
import logging
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.elitea_mcp import config as config_module
from src.elitea_mcp import main as main_module
from src.elitea_mcp import settings_form
from src.elitea_mcp import tray as tray_module
from src.elitea_mcp.commands import bootstrap_servers as bootstrap_servers_module
from src.elitea_mcp.config_errors import ConfigError
from src.elitea_mcp.settings_form import (ConnectionForm, SettingsExit, check_connection, describe_stored_token,
                                          describe_tk_install, find_field_errors, load_form, resolve_token, save_form)
from src.elitea_mcp.tray_platforms import TrayPlatform, WindowsTray
from src.elitea_mcp.utils import logging_setup
from src.elitea_mcp.utils import preflight

REPO = Path(__file__).resolve().parents[1]
SENTINEL_TOKEN = "sentinel-7079-token-value"
SAVED_TOKEN = "saved-7079-token-abcd"
DUPLICATED = '{"servers": {"github": {"command": "npx"}, "github": {"command": "uvx"}}, "auth_token": "t"}'


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "get_config_file", lambda: path)
    monkeypatch.setattr(tray_module, "get_config_file", lambda: path)
    monkeypatch.setattr(bootstrap_servers_module, "get_config_file", lambda: path)
    return path


def write_config(path, config):
    path.write_text(json.dumps(config), encoding="utf-8")


def make_form(**values):
    defaults = {"deployment_url": "https://dev.elitea.ai", "auth_token": "", "project_id": "13", "stored_token": SAVED_TOKEN}
    return ConnectionForm(**{**defaults, **values})


def build_tray(tmp_path, servers_running=False):
    app = tray_module.MCPTrayApp.__new__(tray_module.MCPTrayApp)
    app.icon = None
    app._log_file = tmp_path / "logs" / "tray.log"
    app.platform = TrayPlatform()
    app.platform.open_file = MagicMock()
    app.server_controller = MagicMock(is_running=servers_running)
    app.is_server_running = servers_running
    app.settings_running = False
    app.settings = None
    app.config_error = None
    app.config_report = None
    app.update_menu = MagicMock()
    app.show_error = MagicMock()
    app.show_notification = MagicMock()
    app.restart_server = MagicMock()
    app.update_checker = None
    app.diagnostics_running = False
    app.has_duplicate_keys = False
    return app


def fake_window(exit_code, output="", seconds=0.0):
    """A stand-in for the Settings process: prints output, waits and exits with the given code."""
    script = f"import sys, time; print({output!r}); time.sleep({seconds}); sys.exit({exit_code})"
    return lambda: [sys.executable, "-c", script]


def run_settings(app, monkeypatch, exit_code, output="", seconds=0.0):
    monkeypatch.setattr(tray_module, "build_settings_command", fake_window(exit_code, output, seconds))
    app.run_settings_window()


# 1. No tray callback reads stdin.

def test_no_function_in_the_tray_reads_the_terminal():
    tree = ast.parse((REPO / "src/elitea_mcp/tray.py").read_text())
    called = {node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
              for node in ast.walk(tree) if isinstance(node, ast.Call)}
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert "input" not in called
    assert "interactive_bootstrap" not in called | imported


def test_settings_takes_the_place_of_the_terminal_bootstrap(config_file, tmp_path):
    write_config(config_file, {"deployment_url": "https://dev.elitea.ai", "auth_token": "t"})
    app = build_tray(tmp_path)
    app._read_config()
    configuration = next(item for item in app.build_menu()[0] if item.text == "Configuration")
    assert [item.text for item in configuration.submenu.items] == [
        "Open Config File", "Validate Config", "Open Config Folder", "Settings…", "View Current Config"]


# 3. One window at a time; 11. Quit and Update never wait for it; 12. a broken file is shown, not a window.

def test_the_window_is_waited_on_by_a_daemon_thread_and_a_second_click_says_where_it_is(config_file, tmp_path, monkeypatch):
    write_config(config_file, {"deployment_url": "https://dev.elitea.ai", "auth_token": "t"})
    app = build_tray(tmp_path)
    monkeypatch.setattr(tray_module, "build_settings_command", fake_window(SettingsExit.CANCELLED, seconds=1.5))
    started = time.monotonic()

    app.open_settings()
    app.open_settings()

    assert time.monotonic() - started < 0.5
    waiter = next(thread for thread in threading.enumerate() if thread.name == "settings")
    assert waiter.daemon
    assert [call.args for call in app.show_notification.call_args_list] == [
        ("Settings", "Settings is already open; find it in the taskbar.")]
    waiter.join(10)
    assert not app.settings_running


def test_the_already_open_notice_names_where_this_platform_keeps_the_window(tmp_path):
    app = build_tray(tmp_path)
    app.platform.open_window_hint = "in the Dock or with Cmd+Tab"
    app.settings_running = True
    app.open_settings()
    app.show_notification.assert_called_once_with("Settings", "Settings is already open; find it in the Dock or with Cmd+Tab.")


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS tray needs AppKit")
def test_on_macos_the_open_window_is_found_in_the_dock():
    from src.elitea_mcp.tray_platforms.macos import MacOSTray
    assert MacOSTray.open_window_hint == "in the Dock or with Cmd+Tab"


def test_quit_does_not_wait_for_an_open_settings_window(tmp_path):
    script = f"""
import sys, threading, time
sys.path.insert(0, {str(REPO)!r})
from unittest.mock import MagicMock
from src.elitea_mcp import tray
app = tray.MCPTrayApp.__new__(tray.MCPTrayApp)
app.is_server_running = False
app.settings_running = False
app._read_config = lambda: {{}}
app.config_error = None
app.update_menu = MagicMock()
app.platform = tray.TrayPlatform() if hasattr(tray, "TrayPlatform") else MagicMock(child_process_options=lambda: {{}})
tray.build_settings_command = lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
app.open_settings()
time.sleep(0.3)
"""
    started = time.monotonic()
    subprocess.run([sys.executable, "-c", script], timeout=25, check=True)
    assert time.monotonic() - started < 10


def test_a_broken_config_is_reported_and_opened_instead_of_a_window(config_file, tmp_path, monkeypatch):
    config_file.write_text('{"a": 1,}', encoding="utf-8")
    app = build_tray(tmp_path)
    app.report_config_error = MagicMock()
    popen = MagicMock()
    monkeypatch.setattr(tray_module.subprocess, "Popen", popen)

    app.open_settings()

    app.report_config_error.assert_called_once()
    app.platform.open_file.assert_called_once_with(config_file)
    popen.assert_not_called()
    assert not app.settings_running


def test_a_config_with_a_value_problem_still_opens_the_window_that_can_fix_it(config_file, tmp_path, monkeypatch):
    write_config(config_file, {"deployment_url": "https://dev.elitea.ai", "auth_token": "t", "timeout": 0})
    app = build_tray(tmp_path)
    monkeypatch.setattr(tray_module, "build_settings_command", fake_window(SettingsExit.CANCELLED))

    app.open_settings()

    assert app.config_error is not None
    next(thread for thread in threading.enumerate() if thread.name == "settings").join(10)
    app.platform.open_file.assert_not_called()


# 4. Test connection: one line per step, notes, never raises.

class FakePlatform(http.server.BaseHTTPRequestHandler):
    answers = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        for prefix, (status, content_type) in self.answers.items():
            if self.path.startswith(prefix):
                break
        else:
            status, content_type = 200, "application/json"
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.end_headers()
        self.wfile.write(b"0" if content_type.startswith("text/plain") else b"{}")


@pytest.fixture
def platform_url():
    FakePlatform.answers = {"/socket.io/": (200, "text/plain; charset=UTF-8")}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakePlatform)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_each_check_gets_its_own_line_with_the_platform_answer(platform_url):
    FakePlatform.answers["/api/v2/elitea_core/tags/"] = (403, "application/json")

    lines = check_connection(make_form(deployment_url=platform_url, auth_token=SENTINEL_TOKEN))

    assert [(line.passed, line.title) for line in lines] == [
        (True, "Deployment URL"), (True, "Project ID"), (True, "Platform reachable"), (True, "Token"), (False, "Project access")]
    assert "no access to project 13" in lines[-1].text


def test_test_and_save_use_the_saved_token_when_the_field_is_empty(platform_url, config_file):
    write_config(config_file, {"auth_token": SAVED_TOKEN})
    form = load_form()
    form.deployment_url, form.project_id = platform_url, "13"
    seen = []
    original = preflight._get
    with patch.object(preflight, "_get", side_effect=lambda url, origin, verify, headers=None: (
            seen.append(headers), original(url, origin, verify, headers))[1]):
        lines = check_connection(form)

    assert form.auth_token == "" and resolve_token(form) == SAVED_TOKEN
    assert {"Authorization": f"Bearer {SAVED_TOKEN}"} in seen
    assert all(line.passed for line in lines if line.passed is not None)
    save_form(form)
    assert json.loads(config_file.read_text())["auth_token"] == SAVED_TOKEN


@pytest.mark.parametrize("values", [
    {"auth_token": SENTINEL_TOKEN + "\n"},
    {"auth_token": "abc\u200bdef"},
    {"auth_token": "abc\tdef"},
    {"deployment_url": "http://local host"},
    {"deployment_url": "http://127.0.0.1:99999"},
])
def test_the_connection_test_never_raises_on_pasted_input(values, platform_url):
    lines = check_connection(make_form(**{"deployment_url": platform_url, **values}))
    assert lines and SENTINEL_TOKEN not in " ".join(line.text for line in lines)


def test_an_unexpected_error_in_the_checks_becomes_one_line_naming_only_its_type(monkeypatch):
    monkeypatch.setattr(settings_form, "check_platform", MagicMock(side_effect=ValueError(f"header {SENTINEL_TOKEN}")))

    lines = check_connection(make_form(auth_token=SENTINEL_TOKEN))

    assert [(line.passed, line.text) for line in lines] == [(False, "Stopped by an unexpected ValueError; see the log.")]


def test_an_unexpected_test_error_is_logged_masked_in_the_window_output(capsys, monkeypatch):
    logging_setup.setup_logging(logging_setup.CLI)
    monkeypatch.setattr(settings_form, "check_platform", MagicMock(side_effect=ValueError(f"header {SENTINEL_TOKEN}")))

    check_connection(make_form(auth_token=SENTINEL_TOKEN))

    output = capsys.readouterr().out
    assert "Connection test stopped by an unexpected error" in output and "ValueError" in output
    assert SENTINEL_TOKEN not in output


@pytest.mark.parametrize("url, note", [
    ("http://platform.example", "The token is sent unencrypted over http."),
    ("https://platform.example", 'The TLS certificate is not verified ("ssl_verify" is off).'),
])
def test_the_result_says_when_the_token_travels_unprotected(url, note, monkeypatch):
    monkeypatch.setattr(settings_form, "check_platform", MagicMock(return_value=MagicMock(checks=[])))
    assert [line.text for line in check_connection(make_form(deployment_url=url))] == [note]


def test_a_local_http_platform_or_a_verified_certificate_has_no_note(monkeypatch):
    monkeypatch.setattr(settings_form, "check_platform", MagicMock(return_value=MagicMock(checks=[])))
    assert check_connection(make_form(deployment_url="http://localhost:8080")) == []
    assert check_connection(make_form(deployment_url="https://platform.example", ssl_verify=True)) == []


@pytest.mark.parametrize("values, field, message", [
    ({"deployment_url": ""}, "deployment_url", "Enter the address"),
    ({"deployment_url": "dev.elitea.ai"}, "deployment_url", "starting with https://"),
    ({"deployment_url": "https://[abc"}, "deployment_url", "starting with https://"),
    ({"deployment_url": "http://[::1"}, "deployment_url", "starting with https://"),
    ({"deployment_url": "http://local host"}, "deployment_url", "cannot contain spaces"),
    ({"deployment_url": "http://dev.elitea.ai:99999"}, "deployment_url", "port"),
    ({"deployment_url": "http://dev.elitea.ai:0"}, "deployment_url", "port"),
    ({"deployment_url": "https://me:pw@dev.elitea.ai"}, "deployment_url", "user name and password"),
    ({"auth_token": "abc def"}, "auth_token", "hidden characters"),
    ({"auth_token": "abc\u200bdef"}, "auth_token", "hidden characters"),
    ({"auth_token": "", "stored_token": ""}, "auth_token", "Paste a token"),
    ({"project_id": ""}, "project_id", "Enter the project ID"),
    ({"project_id": "abc"}, "project_id", "positive number"),
    ({"project_id": "0"}, "project_id", "positive number"),
    ({"project_id": "\u00b2"}, "project_id", "positive number"),
])
def test_each_field_names_its_own_problem(values, field, message):
    errors = find_field_errors(make_form(**values))
    assert list(errors) == [field] and message in errors[field]


def test_a_pasted_token_with_a_line_break_at_the_end_is_accepted_as_the_token():
    form = make_form(auth_token=f"  {SENTINEL_TOKEN}\n")
    assert find_field_errors(form) == {} and resolve_token(form) == SENTINEL_TOKEN


# 5. Save writes only the three keys; refuses a file it would damage.

def test_save_changes_only_the_connection_keys(config_file):
    original = {"$schema": "./config.schema.json", "deployment_url": "https://old.example", "auth_token": SAVED_TOKEN,
                "project_id": 1, "servers": {"a": {"command": "npx"}}, "ssl_verify": True, "log_level": "debug"}
    write_config(config_file, original)

    save_form(make_form(deployment_url="https://dev.elitea.ai/app/13/chat", auth_token=SENTINEL_TOKEN))

    assert json.loads(config_file.read_text()) == {**original, "deployment_url": "https://dev.elitea.ai",
                                                   "auth_token": SENTINEL_TOKEN, "project_id": 13}


def test_save_creates_a_private_file_with_its_schema(config_file):
    save_form(make_form(auth_token=SENTINEL_TOKEN))
    assert json.loads(config_file.read_text()) == {"$schema": "./config.schema.json", "deployment_url": "https://dev.elitea.ai",
                                                   "auth_token": SENTINEL_TOKEN, "project_id": 13}
    if os.name != "nt":
        assert stat.S_IMODE(config_file.stat().st_mode) == 0o600


@pytest.mark.parametrize("text", ['{"a": 1,}', DUPLICATED])
def test_save_refuses_a_file_a_rewrite_would_damage_and_changes_no_byte(config_file, text):
    config_file.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        save_form(make_form(auth_token=SENTINEL_TOKEN))
    assert config_file.read_text() == text
    assert not config_module.backup_file_for(config_file).exists()


def test_a_refused_save_names_every_repeated_key(config_file):
    config_file.write_text(DUPLICATED, encoding="utf-8")
    with pytest.raises(ConfigError, match="servers.github appears more than once"):
        save_form(make_form())


def test_an_empty_token_field_keeps_a_token_changed_in_the_file_while_the_window_was_open(config_file):
    write_config(config_file, {"deployment_url": "https://dev.elitea.ai", "auth_token": SAVED_TOKEN, "project_id": 13})
    form = load_form()
    write_config(config_file, {"deployment_url": "https://dev.elitea.ai", "auth_token": "rotated-7079-token", "project_id": 13})

    save_form(form)
    assert json.loads(config_file.read_text())["auth_token"] == "rotated-7079-token"

    form.auth_token = SENTINEL_TOKEN
    save_form(form)
    assert json.loads(config_file.read_text())["auth_token"] == SENTINEL_TOKEN


def test_an_empty_token_field_restores_the_opened_token_when_the_file_lost_it(config_file):
    write_config(config_file, {"deployment_url": "https://dev.elitea.ai", "auth_token": SAVED_TOKEN, "project_id": 13})
    form = load_form()
    write_config(config_file, {"deployment_url": "https://dev.elitea.ai", "auth_token": None, "project_id": 13})
    save_form(form)
    assert json.loads(config_file.read_text())["auth_token"] == SAVED_TOKEN


# 6. The token field opens empty; the saved one is described beside it.

def test_the_window_opens_with_an_empty_token_field_and_the_saved_token_described(config_file):
    write_config(config_file, {"deployment_url": "https://dev.elitea.ai", "auth_token": SAVED_TOKEN, "project_id": 13})

    form = load_form()

    assert (form.deployment_url, form.auth_token, form.project_id) == ("https://dev.elitea.ai", "", "13")
    assert describe_stored_token(form) == "Saved: ********abcd"
    assert describe_stored_token(make_form(stored_token="")) == "No token saved"


# 7–9. How the tray acts on the window's exit code.

def test_a_save_with_restart_restarts_running_servers_once(config_file, tmp_path, monkeypatch):
    write_config(config_file, {"servers": {"a": {"command": "npx"}}})
    app = build_tray(tmp_path, servers_running=True)
    app.settings = {"servers": {"a": {}}}

    run_settings(app, monkeypatch, SettingsExit.SAVED_RESTART)

    app.restart_server.assert_called_once_with()
    app.show_notification.assert_called_once_with("Settings Saved", "Connection saved.")


def test_servers_started_while_the_window_was_open_are_restarted_on_save(config_file, tmp_path, monkeypatch):
    app = build_tray(tmp_path, servers_running=False)
    app.settings = {"servers": {"a": {}}}
    monkeypatch.setattr(tray_module, "build_settings_command", fake_window(SettingsExit.SAVED_RESTART, seconds=0.5))
    app.open_settings()
    app.is_server_running = True
    next(thread for thread in threading.enumerate() if thread.name == "settings").join(10)
    app.restart_server.assert_called_once_with()


def test_a_save_without_restart_or_with_stopped_servers_restarts_nothing(config_file, tmp_path, monkeypatch):
    app = build_tray(tmp_path, servers_running=True)
    run_settings(app, monkeypatch, SettingsExit.SAVED)
    stopped = build_tray(tmp_path, servers_running=False)
    run_settings(stopped, monkeypatch, SettingsExit.SAVED_RESTART)
    app.restart_server.assert_not_called()
    stopped.restart_server.assert_not_called()


def test_a_legacy_mcp_servers_block_counts_as_configured_servers(config_file, tmp_path, monkeypatch):
    write_config(config_file, {"mcpServers": {"a": {"command": "npx"}}})
    app = build_tray(tmp_path)
    run_settings(app, monkeypatch, SettingsExit.SAVED)
    app.show_notification.assert_called_once_with("Settings Saved", "Connection saved.")


def test_the_window_output_is_logged_after_every_close(config_file, tmp_path, monkeypatch):
    log_file = tmp_path / "logs" / "tray.log"
    logging_setup.setup_logging(logging_setup.TRAY, None, log_file)
    run_settings(build_tray(tmp_path), monkeypatch, SettingsExit.SAVED, output="a note from the window")
    assert "a note from the window" in log_file.read_text()


def test_a_save_with_no_servers_says_what_to_do_next(config_file, tmp_path, monkeypatch):
    app = build_tray(tmp_path)
    run_settings(app, monkeypatch, SettingsExit.SAVED)
    app.show_notification.assert_called_once_with(
        "Settings Saved", "Connection saved. Add at least one server in config.json (Configuration → Open Config File), then Start.")


@pytest.mark.parametrize("exit_code, message", [
    (SettingsExit.TK_MISSING, "needs Tk, which this Python lacks"),
    (SettingsExit.NO_DISPLAY, "needs a graphical display"),
])
def test_without_tk_or_a_display_the_config_file_opens_private(exit_code, message, config_file, tmp_path, monkeypatch):
    app = build_tray(tmp_path)
    run_settings(app, monkeypatch, exit_code)
    assert message in app.show_error.call_args.args[1]
    app.platform.open_file.assert_called_once_with(config_file)
    if os.name != "nt":
        assert stat.S_IMODE(config_file.stat().st_mode) == 0o600


def test_open_config_file_makes_an_existing_readable_file_private(config_file, tmp_path):
    write_config(config_file, {"auth_token": SAVED_TOKEN})
    config_file.chmod(0o644)
    build_tray(tmp_path).open_config_file()
    if os.name != "nt":
        assert stat.S_IMODE(config_file.stat().st_mode) == 0o600


def test_a_crashed_window_is_reported_and_its_output_logged_masked_even_for_a_config_with_problems(
        config_file, tmp_path, monkeypatch):
    write_config(config_file, {"auth_token": SAVED_TOKEN, "timeout": 0})
    log_file = tmp_path / "logs" / "tray.log"
    logging_setup.setup_logging(logging_setup.TRAY, None, log_file)
    app = build_tray(tmp_path)

    run_settings(app, monkeypatch, 1, output=f"Traceback: boom {SAVED_TOKEN}")

    assert not app.settings_running
    app.show_error.assert_called_once_with("Settings", "Settings closed unexpectedly. See the log.")
    logged = log_file.read_text()
    assert "boom" in logged and SAVED_TOKEN not in logged


def test_a_cancelled_window_says_nothing(config_file, tmp_path, monkeypatch):
    app = build_tray(tmp_path)
    run_settings(app, monkeypatch, SettingsExit.CANCELLED)
    app.show_notification.assert_not_called()
    app.show_error.assert_not_called()


# 10. No secret in the command line; the window masks every token it holds.

def test_the_window_command_carries_no_secret_and_ignores_the_working_folder():
    assert tray_module.build_settings_command() == [sys.executable, "-P", "-m", "elitea_mcp.main", "settings", "--from-tray"]


def test_a_typed_token_is_masked_in_the_window_log_before_it_is_sent(capsys, monkeypatch):
    logging_setup.setup_logging(logging_setup.CLI)
    monkeypatch.setattr(settings_form, "check_platform",
                        MagicMock(side_effect=lambda config, notify: logging.getLogger("elitea_mcp.x").error(
                            f"sending {config['auth_token']}") or MagicMock(checks=[])))

    check_connection(make_form(auth_token=SENTINEL_TOKEN, stored_token=""))

    output = capsys.readouterr().out
    assert "sending" in output and SENTINEL_TOKEN not in output


def test_the_platform_starts_the_window_without_a_console_on_windows_only():
    assert TrayPlatform().child_process_options() == {}
    if os.name == "nt":
        assert WindowsTray().child_process_options() == {"creationflags": subprocess.CREATE_NO_WINDOW}


# 13. Nothing the tray or the CLI imports needs Tk.

def test_the_client_imports_on_a_python_without_tk():
    script = ("import sys; sys.modules['tkinter'] = None; sys.modules['_tkinter'] = None; "
              "import elitea_mcp.tray, elitea_mcp.main, elitea_mcp.settings_form, elitea_mcp.settings_window; "
              "from elitea_mcp.settings_window import run_settings_window; print(run_settings_window())")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, cwd=REPO / "src")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == str(SettingsExit.TK_MISSING)


@pytest.mark.parametrize("platform_name, prefix, os_release, hint", [
    ("darwin", "/opt/homebrew/Cellar/python@3.13/3.13.1/Frameworks", "", "brew install python-tk@"),
    ("linux", "/usr", 'ID=ubuntu\nID_LIKE=debian\n', "sudo apt install python3-tk"),
    ("linux", "/usr", 'ID="fedora"\n', "sudo dnf install python3-tkinter"),
    ("win32", "C:\\Python312", "", "tcl/tk and IDLE"),
    ("darwin", "/Library/Frameworks/Python.framework", "", "Install Tk support for this Python."),
])
def test_the_install_hint_matches_the_python(platform_name, prefix, os_release, hint, tmp_path):
    release = tmp_path / "os-release"
    release.write_text(os_release)
    assert hint in describe_tk_install(platform_name, prefix, release)


def test_a_venv_made_from_homebrew_python_gets_the_brew_hint(monkeypatch):
    monkeypatch.setattr(settings_form.sys, "platform", "darwin")
    monkeypatch.setattr(settings_form.sys, "prefix", "/Users/me/.local/pipx/venvs/elitea-mcp")
    monkeypatch.setattr(settings_form.sys, "base_prefix", "/opt/homebrew/opt/python@3.13/Frameworks/Python.framework/Versions/3.13")
    assert "brew install python-tk@" in describe_tk_install()


# 14. The CLI bootstrap refuses a config with a repeated key before it asks anything.

def test_interactive_bootstrap_refuses_a_repeated_key_before_the_first_prompt(config_file, monkeypatch):
    config_file.write_text(DUPLICATED, encoding="utf-8")
    monkeypatch.setattr(builtins, "input", MagicMock(side_effect=AssertionError("prompted")))

    with pytest.raises(ConfigError, match="servers.github appears more than once"):
        main_module.bootstrap()

    assert config_file.read_text() == DUPLICATED
    assert not config_module.backup_file_for(config_file).exists()


def test_bootstrap_with_flags_exits_1_and_changes_nothing(config_file, monkeypatch, capsys):
    config_file.write_text(DUPLICATED, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["elitea-mcp", "bootstrap", "--deployment_url", "https://x", "--auth_token", "t"])

    with pytest.raises(SystemExit) as exit_info:
        main_module.cli()

    assert exit_info.value.code == 1
    assert "servers.github appears more than once" in capsys.readouterr().err
    assert config_file.read_text() == DUPLICATED


def test_the_server_wizard_refuses_a_repeated_key_and_keeps_every_other_server(config_file, monkeypatch):
    monkeypatch.setattr(bootstrap_servers_module, "_prompt_int_or_empty", lambda prompt: 13)
    monkeypatch.setattr(bootstrap_servers_module, "_servers_flow", lambda: {"new": {"command": "npx"}})
    config_file.write_text(DUPLICATED, encoding="utf-8")
    with pytest.raises(ConfigError):
        bootstrap_servers_module.bootstrap_servers({})
    assert config_file.read_text() == DUPLICATED

    write_config(config_file, {"mcpServers": {"old": {"command": "uvx"}}, "log_level": "debug"})
    bootstrap_servers_module.bootstrap_servers({})
    assert json.loads(config_file.read_text()) == {"log_level": "debug", "project_id": 13,
                                                   "servers": {"old": {"command": "uvx"}, "new": {"command": "npx"}}}


# The public `elitea-mcp settings` command speaks ordinary exit codes; only the tray reads the window's own.

def run_settings_command(monkeypatch, window_code, *flags):
    monkeypatch.setattr(main_module, "run_settings_window", lambda: window_code)
    monkeypatch.setattr(sys, "argv", ["elitea-mcp", "settings", *flags])
    with pytest.raises(SystemExit) as exit_info:
        main_module.run_cli()
    return exit_info.value.code


@pytest.mark.parametrize("window_code, cli_code", [(SettingsExit.CANCELLED, 0), (SettingsExit.SAVED, 0), (SettingsExit.SAVED_RESTART, 0), (SettingsExit.TK_MISSING, 1), (SettingsExit.NO_DISPLAY, 1)])
def test_the_settings_command_exits_0_after_a_save_or_cancel_and_1_when_no_window_opened(window_code, cli_code, monkeypatch):
    assert run_settings_command(monkeypatch, window_code) == cli_code
    assert run_settings_command(monkeypatch, window_code, "--from-tray") == window_code


@pytest.mark.parametrize("window_code, reason", [(SettingsExit.TK_MISSING, "needs Tk, which this Python lacks"), (SettingsExit.NO_DISPLAY, "needs a graphical display")])
def test_the_settings_command_says_why_no_window_opened(window_code, reason, monkeypatch, capsys):
    run_settings_command(monkeypatch, window_code)
    assert reason in capsys.readouterr().err


# The debug-logging switch keeps writing nothing when nothing changes.

@pytest.mark.parametrize("original", ['{"auth_token": "t"}', '{"auth_token": "t", "log_level": " INFO "}'])
def test_the_debug_switch_writes_nothing_when_the_level_is_already_info(original, config_file):
    config_file.write_text(original, encoding="utf-8")
    tray_module.save_log_level("info")
    assert config_file.read_text() == original
    assert not config_module.backup_file_for(config_file).exists()


def test_the_debug_switch_keeps_its_own_refusal(config_file):
    config_file.write_text(DUPLICATED, encoding="utf-8")
    with pytest.raises(ConfigError, match="Debug logging was not saved, so no key is lost"):
        tray_module.save_log_level("debug")


def test_the_missing_setting_hints_name_every_way_to_set_them():
    with pytest.raises(preflight.PreflightError) as project_error:
        preflight.validate_project_id(None)
    with pytest.raises(preflight.PreflightError) as token_error:
        preflight.validate_auth_token("")
    assert str(project_error.value) == ('"project_id" is not set. Open Settings in the tray, run elitea-mcp bootstrap, '
                                        'or add "project_id": <number> to config.')
    assert str(token_error.value) == ('"auth_token" is not set. Open Settings in the tray, run elitea-mcp bootstrap, '
                                      'or add "auth_token" to config.')
