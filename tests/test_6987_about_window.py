import builtins
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.elitea_mcp import about_window
from src.elitea_mcp import main as main_module
from src.elitea_mcp import tray as tray_module
from src.elitea_mcp.settings_form import SettingsExit
from src.elitea_mcp.tray_platforms import TrayPlatform

REPO = Path(__file__).resolve().parents[1]


def build_tray():
    app = tray_module.MCPTrayApp.__new__(tray_module.MCPTrayApp)
    app.icon = None
    app.platform = TrayPlatform()
    app.about_running = False
    app.show_notification = MagicMock()
    return app


def fake_window(exit_code, seconds=0.0):
    """A stand-in for the About process: waits, then exits with the given code."""
    return lambda: [sys.executable, "-c", f"import sys, time; time.sleep({seconds}); sys.exit({exit_code})"]


def wait_for_about(app):
    waiter = next((thread for thread in threading.enumerate() if thread.name == "about"), None)
    if waiter:
        waiter.join(10)
    return waiter


def test_about_opens_a_window_process_and_returns_at_once(monkeypatch):
    app = build_tray()
    monkeypatch.setattr(tray_module, "build_about_command", fake_window(SettingsExit.CANCELLED, seconds=1.0))
    started = time.monotonic()

    app.show_about()

    assert time.monotonic() - started < 0.5
    assert app.about_running
    waiter = wait_for_about(app)
    assert waiter.daemon
    assert not app.about_running
    app.show_notification.assert_not_called()


def test_the_window_process_gets_the_platform_launch_options(monkeypatch):
    app = build_tray()
    app.platform.child_process_options = lambda: {"env": {"MARK": "6987"}}
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, b""))
    monkeypatch.setattr(tray_module.subprocess, "run", run)

    app.run_about_window()

    assert run.call_args.args[0] == [sys.executable, "-P", "-m", "elitea_mcp.main", "about", "--from-tray"]
    assert run.call_args.kwargs["env"] == {"MARK": "6987"}
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL


def test_a_second_click_while_the_window_is_open_says_where_it_is(monkeypatch):
    app = build_tray()
    monkeypatch.setattr(tray_module, "build_about_command", fake_window(SettingsExit.CANCELLED, seconds=1.0))

    app.show_about()
    app.show_about()

    assert [thread.name for thread in threading.enumerate()].count("about") == 1
    assert [call.args for call in app.show_notification.call_args_list] == [
        ("About", "About is already open; find it in the taskbar.")]
    wait_for_about(app)


@pytest.mark.parametrize("exit_code", [SettingsExit.TK_MISSING, SettingsExit.NO_DISPLAY, 1])
def test_without_a_window_about_falls_back_to_a_notification_with_the_version(monkeypatch, exit_code):
    app = build_tray()
    monkeypatch.setattr(tray_module, "build_about_command", fake_window(exit_code))
    monkeypatch.setattr(about_window, "get_installed_version", lambda: "0.1.36")

    app.run_about_window()

    title, message = app.show_notification.call_args.args
    assert title == "ELITEA MCP Client"
    assert "Version: 0.1.36" in message
    assert not app.about_running


def test_a_window_process_that_cannot_start_falls_back_to_a_notification(monkeypatch):
    app = build_tray()
    monkeypatch.setattr(tray_module, "build_about_command", lambda: [str(REPO / "no-such-python")])

    app.run_about_window()

    assert "Version:" in app.show_notification.call_args.args[1]
    assert not app.about_running


def test_about_names_the_version_or_a_development_checkout(monkeypatch):
    monkeypatch.setattr(about_window, "get_installed_version", lambda: "0.1.36")
    assert about_window.describe_about()[0] == "Version: 0.1.36"
    monkeypatch.setattr(about_window, "get_installed_version", lambda: None)
    assert about_window.describe_about()[0] == "Version: unknown (development checkout)"


def test_without_tk_the_command_prints_the_text_and_says_why(monkeypatch, capsys):
    real_import = builtins.__import__

    def no_tkinter(name, *args, **kwargs):
        if name == "tkinter":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_tkinter)
    monkeypatch.setattr(about_window, "get_installed_version", lambda: "0.1.36")

    assert about_window.run_about_window() == SettingsExit.TK_MISSING
    assert "Version: 0.1.36" in capsys.readouterr().out


@pytest.mark.parametrize("exit_code", [SettingsExit.CANCELLED, SettingsExit.TK_MISSING, SettingsExit.NO_DISPLAY])
def test_the_about_command_succeeds_in_a_terminal_and_passes_the_window_code_to_the_tray(monkeypatch, exit_code):
    monkeypatch.setattr(main_module, "install_crash_hooks", lambda: None)
    monkeypatch.setattr(main_module, "run_about_window", lambda: exit_code)

    monkeypatch.setattr(sys, "argv", ["elitea-mcp", "about"])
    with pytest.raises(SystemExit) as from_terminal:
        main_module.cli()
    monkeypatch.setattr(sys, "argv", ["elitea-mcp", "about", "--from-tray"])
    with pytest.raises(SystemExit) as from_tray:
        main_module.cli()

    assert from_terminal.value.code == 0
    assert from_tray.value.code == exit_code


WINDOW_PROBE = """
import json, sys, tkinter
from tkinter import ttk
from elitea_mcp import about_window
about_window.get_installed_version = lambda: "0.1.36"
try:
    root = tkinter.Tk()
except tkinter.TclError as error:
    print(json.dumps({"no_display": str(error)}))
    sys.exit()
about_window.build_about_window(tkinter, ttk, root)
root.update()
def texts(widget):
    found = [widget.cget("text")] if widget.winfo_class() in ("TLabel", "TButton") and widget.cget("text") else []
    return found + [text for child in widget.winfo_children() for text in texts(child)]
shown = {"title": root.title(), "texts": texts(root),
         "has_logo": any(child.cget("image") for frame in root.winfo_children() for child in frame.winfo_children()
                         if child.winfo_class() == "TLabel")}
root.event_generate("<Escape>")
try:
    root.update()
    shown["closed_by_escape"] = not root.winfo_exists()
except tkinter.TclError:
    shown["closed_by_escape"] = True
print(json.dumps(shown))
"""


def test_the_window_shows_the_name_version_and_licence_and_closes_on_escape():
    pytest.importorskip("tkinter")
    result = subprocess.run([sys.executable, "-c", WINDOW_PROBE], capture_output=True, text=True, timeout=60,
                            env={**os.environ, "PYTHONPATH": str(REPO / "src")})
    shown = json.loads(result.stdout)
    if "no_display" in shown:
        pytest.skip(f"no display: {shown['no_display']}")
    assert shown["title"] == "About ELITEA MCP Client"
    assert shown["texts"] == ["ELITEA MCP Client", "Version: 0.1.36", "Model Context Protocol client for the ELITEA platform.",
                              "Licensed under the Apache License 2.0.", "Close"]
    assert shown["has_logo"]
    assert shown["closed_by_escape"]
