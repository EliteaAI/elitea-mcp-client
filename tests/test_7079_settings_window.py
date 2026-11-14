import json
import subprocess
from importlib import resources
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.elitea_mcp import config as config_module
from src.elitea_mcp import settings_window
from src.elitea_mcp.settings_form import CheckLine, ConnectionForm, SettingsExit

tkinter = pytest.importorskip("tkinter")
ttk = pytest.importorskip("tkinter.ttk")

REPO = Path(__file__).resolve().parents[1]
SAVED_TOKEN = "saved-7079-token-abcd"


@pytest.fixture(scope="module")
def tk_root():
    """One Tk root for the module: Tk 9 on macOS crashes when a second root is created in a process after the first is gone."""
    try:
        shared_root = tkinter.Tk()
    except tkinter.TclError as error:
        pytest.skip(f"no display: {error}")
    shared_root.withdraw()
    yield shared_root
    shared_root.destroy()


@pytest.fixture
def root(tk_root):
    window = tkinter.Toplevel(tk_root)
    yield window
    if window.winfo_exists():
        window.destroy()


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "get_config_file", lambda: path)
    return path


def open_window(root, **values):
    form = ConnectionForm(**{"deployment_url": "https://dev.elitea.ai", "project_id": "13", "stored_token": SAVED_TOKEN, **values})
    return settings_window.SettingsWindow(tkinter, ttk, root, form)


def pump(root, until, seconds=5):
    deadline = time.monotonic() + seconds
    while not until() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    return until()


def test_save_writes_the_form_and_closes_with_saved(root, config_file):
    config_file.write_text(json.dumps({"servers": {"a": {"command": "npx"}}}))
    window = open_window(root)
    window.values["auth_token"].set("typed-7079-token")
    window.restart.set(False)

    window.save()

    assert window.exit_code == SettingsExit.SAVED
    assert json.loads(config_file.read_text()) == {"servers": {"a": {"command": "npx"}}, "deployment_url": "https://dev.elitea.ai",
                                                   "auth_token": "typed-7079-token", "project_id": 13}


@pytest.mark.parametrize("restart, exit_code", [(True, SettingsExit.SAVED_RESTART), (False, SettingsExit.SAVED)])
def test_the_restart_choice_is_always_offered_and_sets_the_exit_code(root, config_file, restart, exit_code):
    window = open_window(root)
    window.restart.set(restart)
    window.save()
    assert window.exit_code == exit_code


def test_an_invalid_field_keeps_the_window_open_and_says_why(root, config_file):
    window = open_window(root, project_id="abc")

    window.save()

    assert window.exit_code == SettingsExit.CANCELLED and root.winfo_exists()
    assert "positive number" in window.field_errors["project_id"].get()
    assert not config_file.exists()


def test_a_refused_save_shows_the_reason_and_changes_nothing(root, config_file):
    text = '{"servers": {"a": {"command": "npx"}, "a": {"command": "uvx"}}}'
    config_file.write_text(text)
    window = open_window(root)

    window.save()

    assert window.exit_code == SettingsExit.CANCELLED and "servers.a appears more than once" in window.status.get()
    assert config_file.read_text() == text


def test_the_token_is_hidden_until_show_is_ticked(root):
    window = open_window(root)
    assert window.token_entry.cget("show") == "•"
    window.show_token.set(True)
    window.toggle_token()
    assert window.token_entry.cget("show") == ""


def test_the_test_runs_off_the_window_thread_and_its_lines_are_shown(root, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(settings_window, "check_connection",
                        lambda form: release.wait(5) and [CheckLine(True, "Token", "accepted"), CheckLine(None, "Note", "n")])
    window = open_window(root)

    window.start_test()
    root.update()

    assert window.results.cget("text") == "Testing…"
    assert next(thread for thread in threading.enumerate() if thread.name == "connection-test").daemon
    release.set()
    assert pump(root, lambda: window.results.cget("text") != "Testing…")
    assert window.results.cget("text") == "✓ Token: accepted\n• Note: n"


def test_closing_the_window_during_a_stalled_test_ends_the_process_at_once():
    script = f"""
import sys, time, tkinter
from tkinter import ttk
sys.path.insert(0, {str(REPO)!r})
from src.elitea_mcp import settings_window
from src.elitea_mcp.settings_form import ConnectionForm
settings_window.check_connection = lambda form: time.sleep(30)
root = tkinter.Tk()
window = settings_window.SettingsWindow(tkinter, ttk, root, ConnectionForm("https://dev.elitea.ai", "", "13", "t"))
window.start_test()
root.after(200, window.cancel)
sys.exit(window.run())
"""
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=25)
    if "no display" in result.stderr or "couldn't connect to display" in result.stderr:
        pytest.skip("no display")
    assert result.returncode == SettingsExit.CANCELLED, result.stderr
    assert time.monotonic() - started < 10


def test_on_macos_the_dock_tile_is_set_once_tk_exists():
    script = f"""
import sys, tkinter
from tkinter import ttk
sys.path.insert(0, {str(REPO)!r})
from src.elitea_mcp import settings_window
from src.elitea_mcp.settings_form import ConnectionForm
calls = []
settings_window.sys.platform = "darwin"
root = tkinter.Tk()
settings_window.show_in_dock_as_elitea = lambda: calls.append(bool(root.winfo_exists()))
window = settings_window.SettingsWindow(tkinter, ttk, root, ConnectionForm("https://dev.elitea.ai", "", "13", "t"))
root.after(50, window.cancel)
print(window.run(), calls)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=25)
    if "display" in result.stderr:
        pytest.skip("no display")
    assert result.stdout.strip().splitlines()[-1] == f"{SettingsExit.CANCELLED} [True]", result.stderr


def test_the_window_records_problems_to_its_output(root, config_file, monkeypatch):
    config_file.write_text('{"a": 1,}')
    logged = MagicMock()
    monkeypatch.setattr(settings_window.logger, "error", logged)
    window = open_window(root)
    window.save()
    assert "Settings were not saved" in logged.call_args.args[0]


def test_without_a_display_the_window_reports_no_display(monkeypatch):
    monkeypatch.setattr(tkinter, "Tk", MagicMock(side_effect=tkinter.TclError("no display name and no $DISPLAY environment variable")))
    assert settings_window.run_settings_window() == SettingsExit.NO_DISPLAY


def test_the_dock_tile_logo_ships_with_the_package():
    assert (resources.files("elitea_mcp.icons") / "logo64x64.png").is_file()
