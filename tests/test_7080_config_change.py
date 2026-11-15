"""#7080: the tray notices config.json changes, says once that a restart is needed, and applies the live settings at once."""
import json
import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.elitea_mcp import config as config_module
from src.elitea_mcp import config_model as config_model_module
from src.elitea_mcp import tray as tray_module
from src.elitea_mcp.utils import package_updates
from src.elitea_mcp.utils import server_controller as controller_module
from src.elitea_mcp.utils import config_watcher as config_watcher_module
from src.elitea_mcp.utils import logging_setup
from src.elitea_mcp.utils.config_watcher import ConfigFileWatcher
from src.elitea_mcp.utils.package_updates import UpdateState

VALID = {"deployment_url": "https://dev.elitea.ai", "auth_token": "tok", "project_id": 2,
         "servers": {"fs": {"command": "npx"}}}
TWO_SERVERS = {**VALID, "servers": {"fs": {"command": "npx"}, "gh": {"command": "uvx"}}}
THREE_SERVERS = {**VALID, "servers": {"fs": {"command": "npx"}, "gh": {"command": "uvx"}, "x": {"command": "node"}}}
KNOWN_UPDATE = UpdateState("0.1.36", "0.1.37")
RESTART_LABEL = "Restart MCP Server"
RESTART_TO_APPLY_LABEL = "Restart MCP Server to Apply Changes"
KEEP_RUNNING = "The servers keep running the previous config."
NO_SERVER_CAN_START = f"config.json changed, but no server in it can start (Validate Config to see why). {KEEP_RUNNING}"
NO_CONNECTION = f"config.json changed, but it has no deployment URL or token (Configuration → Settings…). {KEEP_RUNNING}"


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "get_config_file", lambda: path)
    monkeypatch.setattr(tray_module, "get_config_file", lambda: path)
    path.write_text(json.dumps(VALID))
    return path


@pytest.fixture
def no_pypi(monkeypatch):
    fetches = []
    monkeypatch.setattr(package_updates, "fetch_update_state", lambda: fetches.append(1) or KNOWN_UPDATE)
    return fetches


def running_tray(config_file):
    controller = controller_module.ServerController()
    with patch.object(tray_module, "get_server_controller", return_value=controller):
        app = tray_module.MCPTrayApp()
    app.icon = MagicMock()
    app.platform.run_on_ui_thread = lambda function: function()
    app._update_checks_started = True
    app.notes = []
    app.platform.notify = lambda icon, title, message: app.notes.append((title, message))
    app.show_error = lambda title, message: app.notes.append((title, message))
    controller.is_running = True
    app.is_server_running = True
    controller.config = tray_module.check_config(json.loads(config_file.read_text())).config
    return app, controller


def texts(app):
    return [item.text for item in app.icon.menu.items]


def restart_notes(app):
    return [note for note in app.notes if note[0] == "Restart Needed"]


def write(path, config):
    path.write_text(json.dumps(config))


def test_watcher_fires_once_after_the_signature_settles(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{}")
    calls = []
    watcher = ConfigFileWatcher(path, lambda: calls.append(time.monotonic()), interval=0.05)
    watcher.start()
    time.sleep(0.12)
    path.write_text('{"a": 1}')
    time.sleep(0.4)
    watcher.stop()
    assert len(calls) == 1


def test_distinct_saves_in_one_pending_period_announce_once(config_file, no_pypi):
    app, controller = running_tray(config_file)
    app.update_menu()
    for config in (TWO_SERVERS, THREE_SERVERS, {**THREE_SERVERS, "timeout": 99}, {**THREE_SERVERS, "log_level": "debug"}):
        write(config_file, config)
        app.update_menu()
    assert restart_notes(app) == [("Restart Needed", "config.json changed.")]
    assert RESTART_TO_APPLY_LABEL in texts(app) and RESTART_LABEL not in texts(app)


def test_editing_back_resets_the_label_and_the_next_change_announces_again(config_file, no_pypi):
    app, controller = running_tray(config_file)
    write(config_file, TWO_SERVERS)
    app.update_menu()
    write(config_file, VALID)
    app.update_menu()
    assert RESTART_LABEL in texts(app) and RESTART_TO_APPLY_LABEL not in texts(app)
    write(config_file, THREE_SERVERS)
    app.update_menu()
    assert len(restart_notes(app)) == 2


def test_a_parse_error_inside_a_pending_period_does_not_re_arm_the_notice(config_file, no_pypi):
    app, controller = running_tray(config_file)
    write(config_file, TWO_SERVERS)
    app.update_menu()
    config_file.write_text('{"servers": ')
    app.update_menu()
    write(config_file, THREE_SERVERS)
    app.update_menu()
    assert len(restart_notes(app)) == 1


def test_a_server_entry_with_an_error_gives_the_problem_count(config_file, no_pypi):
    app, controller = running_tray(config_file)
    write(config_file, {**VALID, "servers": {"fs": {"command": "npx"}, "bad": {"type": "token-like-SECRET"}}})
    app.update_menu()
    assert restart_notes(app) == [("Restart Needed", "config.json changed. It has 1 problem (Validate Config to see).")]


def test_no_servers_keeps_the_plain_label_and_restart_is_refused_without_stopping(config_file, no_pypi):
    app, controller = running_tray(config_file)
    controller.stop_servers = MagicMock()
    write(config_file, {**VALID, "servers": {}})
    app.update_menu()
    assert restart_notes(app) == [("Restart Needed", NO_SERVER_CAN_START)]
    assert RESTART_LABEL in texts(app)
    app.restart_server()
    controller.stop_servers.assert_not_called()
    assert app.notes[-1] == ("Restart Refused", NO_SERVER_CAN_START)
    app.update_menu()
    assert len(restart_notes(app)) == 1


def test_removing_the_token_is_announced_with_the_settings_hint(config_file, no_pypi):
    app, controller = running_tray(config_file)
    write(config_file, {k: v for k, v in VALID.items() if k != "auth_token"})
    app.update_menu()
    assert restart_notes(app) == [("Restart Needed", NO_CONNECTION)]


def test_a_later_save_that_leaves_nothing_runnable_is_silent_in_the_same_period(config_file, no_pypi):
    app, controller = running_tray(config_file)
    write(config_file, TWO_SERVERS)
    app.update_menu()
    write(config_file, {**VALID, "servers": {}})
    app.update_menu()
    assert restart_notes(app) == [("Restart Needed", "config.json changed.")]
    assert RESTART_LABEL in texts(app)


def test_restart_uses_the_checked_config_even_when_the_file_breaks_during_the_sleep(config_file, no_pypi, monkeypatch):
    app, controller = running_tray(config_file)
    write(config_file, TWO_SERVERS)
    controller.stop_servers = MagicMock(side_effect=lambda: setattr(controller, "is_running", False))
    started = []
    monkeypatch.setattr(controller_module.time, "sleep", lambda seconds: config_file.write_text('{"servers": '))
    monkeypatch.setattr(controller, "_start_embedded_servers", lambda: started.append(controller.config) or True)
    app.restart_server()
    assert list(started[0]["servers"]) == ["fs", "gh"]


def test_a_redraw_in_flight_when_settings_closes_does_not_prompt(config_file, no_pypi, monkeypatch):
    app, controller = running_tray(config_file)
    in_build, release = threading.Event(), threading.Event()
    original = app.find_pending_restart

    def slow_pending():
        result = original()
        if threading.current_thread().name == "slow-redraw":
            in_build.set()
            release.wait(5)
        return result

    app.find_pending_restart = slow_pending
    app.settings_running = True
    redraw = threading.Thread(target=app.update_menu, name="slow-redraw")
    redraw.start()
    in_build.wait(5)
    write(config_file, TWO_SERVERS)
    monkeypatch.setattr(tray_module, "build_settings_command", lambda: ["true"])
    settings = threading.Thread(target=app.run_settings_window)
    settings.start()
    time.sleep(0.3)
    release.set()
    redraw.join(5)
    settings.join(5)
    app.update_menu()
    assert restart_notes(app) == []
    assert RESTART_TO_APPLY_LABEL in texts(app)


def test_log_level_applies_live_and_the_menu_switch_with_env_debug_stays_off(config_file, no_pypi, monkeypatch):
    app, controller = running_tray(config_file)
    write(config_file, {**VALID, "log_level": "debug"})
    app.update_menu()
    assert tray_module.is_debug_logging() and app.notes == []
    write(config_file, VALID)
    app.update_menu()
    assert not tray_module.is_debug_logging()
    monkeypatch.setenv("ELITEA_MCP_DEBUG", "1")
    tray_module.apply_debug_logging(True)
    for _ in range(3):
        app.toggle_debug_logging()
    assert [tray_module.is_debug_logging(), json.loads(config_file.read_text())["log_level"]] == [False, "info"]


def test_building_an_app_on_a_debug_config_does_not_flip_the_global_logger(config_file, no_pypi):
    write(config_file, {**VALID, "log_level": "debug"})
    app, controller = running_tray(config_file)
    app.update_menu()
    assert logging.getLogger("elitea_mcp").level == logging.NOTSET


def wait_for(condition):
    deadline = time.monotonic() + 3
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


def test_an_interval_change_restarts_the_checker_without_re_announcing_a_known_update(config_file, no_pypi):
    app, controller = running_tray(config_file)
    first = app.update_checker
    first.state = KNOWN_UPDATE
    write(config_file, {**VALID, "update_check_interval_hours": 48})
    app.update_menu()
    second = app.update_checker
    assert second is not first and first._stopped.is_set() and second._interval_seconds == 48 * 3600
    assert wait_for(lambda: no_pypi == [1])
    time.sleep(0.1)
    assert [n for n in app.notes if n[0] == "Update Available"] == [] and restart_notes(app) == []
    assert "Update to 0.1.37 and Restart" in texts(app)
    second.stop()


def test_update_check_off_hides_the_items_and_on_checks_once(config_file, no_pypi):
    app, controller = running_tray(config_file)
    first = app.update_checker
    write(config_file, {**VALID, "update_check": False})
    app.update_menu()
    assert app.update_checker is None and first._stopped.is_set()
    assert "Check for Updates" not in texts(app) and app.notes == []
    write(config_file, VALID)
    app.update_menu()
    assert app.update_checker is not None and wait_for(lambda: no_pypi == [1])
    app.update_menu()
    time.sleep(0.1)
    assert no_pypi == [1] and "Check for Updates" in texts(app)
    app.update_checker.stop()


def test_a_redraw_never_starts_a_check_when_nothing_changed(config_file, no_pypi):
    app, controller = running_tray(config_file)
    for _ in range(3):
        app.update_menu()
    time.sleep(0.1)
    assert no_pypi == []


def test_update_check_off_in_the_save_that_fixes_a_launch_time_schema_error(config_file, no_pypi):
    write(config_file, {**VALID, "timeout": -5})
    with patch.object(tray_module, "get_server_controller", return_value=controller_module.ServerController()):
        app = tray_module.MCPTrayApp()
    app.icon = MagicMock()
    app.platform.run_on_ui_thread = lambda function: function()
    assert app.config_error is not None and app.update_checker is not None
    write(config_file, {**VALID, "update_check": False})
    app.update_menu()
    assert app.update_checker is None


def test_an_old_checker_mid_fetch_does_not_announce_after_an_interval_change(config_file, monkeypatch):
    in_fetch, release = threading.Event(), threading.Event()

    def slow_fetch():
        if threading.current_thread().name == "update-checker" and not in_fetch.is_set():
            in_fetch.set()
            release.wait(5)
        return UpdateState("0.1.36", "0.1.37")

    monkeypatch.setattr(package_updates, "fetch_update_state", slow_fetch)
    app, controller = running_tray(config_file)
    app.update_checker.start()
    in_fetch.wait(5)
    write(config_file, {**VALID, "update_check_interval_hours": 48})
    app.update_menu()
    time.sleep(0.2)
    release.set()
    time.sleep(0.3)
    announced = [n for n in app.notes if n[0] == "Update Available"]
    app.update_checker.stop()
    assert len(announced) == 1


def test_invalid_config_while_running_opens_with_keep_running_and_keeps_stop(config_file, no_pypi):
    app, controller = running_tray(config_file)
    config_file.write_text('{"servers": ')
    app.update_menu()
    assert app.notes[-1][0] == "Invalid Config" and app.notes[-1][1].startswith(KEEP_RUNNING)
    assert "Stop MCP Server" in texts(app) and RESTART_TO_APPLY_LABEL not in texts(app)


def test_quit_stops_the_watcher(config_file, no_pypi):
    app, controller = running_tray(config_file)
    controller.stop_servers = MagicMock()
    app.quit_app()
    assert app.config_watcher._stopped.is_set()


def test_breaking_a_running_server_entry_gives_the_problem_count(config_file, no_pypi):
    app, controller = running_tray(config_file)
    write(config_file, {**VALID, "servers": {"fs": {"command": "npx"}, "gh": {"command": "uvx"}}})
    controller.config = tray_module.check_config(json.loads(config_file.read_text())).config
    write(config_file, {**VALID, "servers": {"fs": {"command": "npx"}, "gh": {"type": "nope"}}})
    app.update_menu()
    assert restart_notes(app) == [("Restart Needed", "config.json changed. It has 1 problem (Validate Config to see).")]


def test_after_a_restart_with_a_skipped_entry_nothing_is_pending(config_file, no_pypi, monkeypatch):
    app, controller = running_tray(config_file)
    write(config_file, {**VALID, "servers": {"fs": {"command": "npx"}, "bad": {"type": "nope"}}})
    controller.stop_servers = MagicMock(side_effect=lambda: setattr(controller, "is_running", False))
    monkeypatch.setattr(controller_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(controller, "_start_embedded_servers", lambda: setattr(controller, "is_running", True) or True)
    app.restart_server()
    app.is_server_running = True
    app.update_menu()
    assert RESTART_LABEL in texts(app) and RESTART_TO_APPLY_LABEL not in texts(app)


def test_a_changed_update_setting_before_the_tray_is_ready_does_not_start_a_check(config_file, no_pypi):
    app, controller = running_tray(config_file)
    app._update_checks_started = False
    write(config_file, {**VALID, "update_check_interval_hours": 48})
    app.update_menu()
    time.sleep(0.1)
    assert no_pypi == [] and app.update_checker._interval_seconds == 48 * 3600


def test_check_for_updates_reports_from_the_checker_it_started_with(config_file, monkeypatch):
    app, controller = running_tray(config_file)
    in_fetch, release = threading.Event(), threading.Event()

    def slow_fetch():
        in_fetch.set()
        release.wait(5)
        return KNOWN_UPDATE

    monkeypatch.setattr(package_updates, "fetch_update_state", slow_fetch)
    app.check_for_updates()
    in_fetch.wait(5)
    write(config_file, {**VALID, "update_check": False})
    app.update_menu()
    release.set()
    assert wait_for(lambda: any(note[0] == "Check for Updates" for note in app.notes))
    assert ("Check for Updates", KNOWN_UPDATE.describe()) in app.notes


@pytest.mark.parametrize("value, hours, warned", [
    (1e-9, 1, True), (0.5, 1, True), (float("nan"), 24, True), (float("inf"), 24, True), (None, 24, True),
    ("abc", 24, True), ([1], 24, True), ({}, 24, True), (True, 24, True), (10 ** 400, 24, True),
    ("12", 12, False), (48, 48, False), (1, 1, False), (0, 0, False), (-1, -1, False),
])
def test_the_update_interval_is_a_finite_number_of_at_least_an_hour(value, hours, warned):
    report = tray_module.check_config({"update_check_interval_hours": value})
    assert report.config["update_check_interval_hours"] == hours
    assert bool(report.notable) == warned


def test_an_interval_warning_never_repeats_the_value():
    report = tray_module.check_config({"update_check_interval_hours": "ghp_SECRETTOKEN"})
    assert "ghp_SECRETTOKEN" not in report.summary


def test_a_project_id_error_never_repeats_the_value():
    report = tray_module.check_config({"project_id": "ghp_SECRETTOKEN"})
    assert report.config is None and "ghp_SECRETTOKEN" not in report.summary


def test_a_launch_with_a_null_interval_builds_a_checker(config_file, no_pypi):
    write(config_file, {**VALID, "update_check_interval_hours": None})
    app, controller = running_tray(config_file)
    assert app.update_checker._interval_seconds == 24 * 3600


def test_the_watcher_waits_until_a_change_holds_for_a_second_poll(monkeypatch, tmp_path):
    signatures = iter(["saved", "half-written", "saved-again", "saved-again", "saved-again"])
    monkeypatch.setattr(config_watcher_module, "read_signature", lambda path: next(signatures, "saved-again"))
    calls = []
    watcher = ConfigFileWatcher(tmp_path / "c.json", lambda: calls.append(1), interval=0)
    polls = iter(range(5))
    monkeypatch.setattr(watcher._stopped, "wait", lambda seconds: next(polls, None) is None)
    watcher._poll(config_watcher_module.read_signature(None))
    assert calls == [1]


def test_a_long_notification_is_cut_to_what_windows_accepts(config_file, no_pypi):
    app, controller = running_tray(config_file)
    app.show_notification("Title", "x" * 400)
    assert len(app.notes[-1][1]) == tray_module.NOTIFICATION_LIMIT and app.notes[-1][1].endswith("...")


def test_a_blocked_config_registers_its_token_before_anything_logs_it(config_file, no_pypi):
    logging_setup.forget_log_secrets()
    write(config_file, {**VALID, "auth_token": "ghp_BLOCKEDCONFIGTOKEN", "project_id": "x"})
    app, controller = running_tray(config_file)
    assert app.config_error is not None
    assert "ghp_BLOCKEDCONFIGTOKEN" not in logging_setup.mask_log_text("token ghp_BLOCKEDCONFIGTOKEN", logging_setup._secrets)


def test_no_notice_while_settings_is_open(config_file, no_pypi):
    app, controller = running_tray(config_file)
    app.settings_running = True
    write(config_file, TWO_SERVERS)
    app.update_menu()
    assert restart_notes(app) == [] and RESTART_TO_APPLY_LABEL in texts(app)


def test_a_redraw_during_a_restart_does_not_announce_the_change_being_applied(config_file, no_pypi):
    app, controller = running_tray(config_file)
    write(config_file, TWO_SERVERS)
    controller.restart_servers = MagicMock(side_effect=lambda raw_config: app.update_menu())
    app.restart_server()
    controller.restart_servers.assert_called_once()
    assert restart_notes(app) == []


def test_the_tray_starts_watching_once_it_is_ready(config_file, no_pypi):
    app, controller = running_tray(config_file)
    app.config_watcher = MagicMock()
    app.update_checker = None
    app._on_tray_ready(MagicMock())
    app.config_watcher.start.assert_called_once_with()


def test_an_interval_longer_than_windows_can_wait_is_held_to_its_limit(monkeypatch):
    monkeypatch.setattr(config_model_module.threading, "TIMEOUT_MAX", 4294967.0)
    held = tray_module.check_config({"update_check_interval_hours": 2160})
    kept = tray_module.check_config({"update_check_interval_hours": 1193})
    assert held.config["update_check_interval_hours"] == 1193
    assert [p.path for p in held.notable] == ["update_check_interval_hours"]
    assert kept.config["update_check_interval_hours"] == 1193 and kept.notable == []


def test_the_longest_interval_is_a_wait_this_platform_accepts():
    hours = tray_module.check_config({"update_check_interval_hours": 10 ** 9}).config["update_check_interval_hours"]
    lock = threading.Lock()
    assert lock.acquire(timeout=hours * 3600)
    lock.release()
