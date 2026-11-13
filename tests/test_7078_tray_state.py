import platform
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.elitea_mcp import tray as tray_module
from src.elitea_mcp.tray import STATUS_DOT_COLORS, TOOLTIP_LIMIT, UPDATE_BADGE_COLOR, fit_status_text
from src.elitea_mcp.utils import server_controller as controller_module
from src.elitea_mcp.utils import server_supervisor as supervisor_module
from src.elitea_mcp.utils.preflight import PreflightError
from src.elitea_mcp.utils.server_loader import COMMAND_NOT_FOUND, ServerFailure, ServerLoad
from src.elitea_mcp.utils.server_registry import DECLINED, REGISTERED, ServerRegistry
from src.elitea_mcp.utils.server_supervisor import ServerSupervisor
from src.elitea_mcp.utils.sio import start_socket_connection
from tests.test_7070_platform_registration import drop_platform, find_free_port, serve_platform, wait_until

PREFIX = "ELITEA MCP Client — "
PLATFORM_ERROR = "Cannot reach https://platform.example"


def build_tray(controller=None, can_update=False):
    """A tray app on a real controller, with its config read and notifications stubbed."""
    with patch.object(tray_module, "get_server_controller", return_value=controller or controller_module.ServerController()), \
            patch.object(tray_module, "UpdateChecker"):
        app = tray_module.MCPTrayApp()
    app._read_config = MagicMock(return_value={"deployment_url": "http://127.0.0.1:9", "auth_token": "token"})
    app.config_error = None
    app.config_report = MagicMock(notable=[])
    app.update_checker = MagicMock(state=MagicMock(can_update=can_update, latest="0.2.0"))
    app.show_notification = MagicMock()
    app.show_error = MagicMock()
    return app


def count_utf16_units(text):
    return len(text.encode("utf-16-le")) // 2


def make_running(controller, entries=(), ready=True):
    """Put the controller in a running session whose supervisor holds the given registry entries."""
    registry = ServerRegistry()
    for name, registration in entries:
        registry.record_load(ServerLoad(name, "stdio", tools=[]))
        registry.apply_registration([name], {name} if registration == REGISTERED else set(),
                                    [{"name": name, "reason": "taken"}] if registration == DECLINED else [])
    controller.is_running = True
    controller.supervisor = MagicMock(cancelled=controller.stop_event, registry=registry, link=MagicMock(ready=ready))


def test_a_client_that_never_ran_is_stopped_with_no_dot():
    assert build_tray().read_client_state() == (None, "stopped")


def test_a_config_error_wins_over_a_live_session():
    app = build_tray()
    make_running(app.server_controller, [("github", REGISTERED)])
    app.config_error = MagicMock(summary="line 1, column 8: Trailing comma before }.")
    assert app.read_client_state() == ("error", "error: line 1, column 8: Trailing comma before }.")


def test_a_run_that_ended_on_an_error_shows_it_until_the_next_start_or_stop():
    app = build_tray()
    app.server_controller.last_error = PLATFORM_ERROR
    assert app.read_client_state() == ("error", f"error: {PLATFORM_ERROR}")


def test_a_daemon_run_is_shown_as_running_elsewhere():
    app = build_tray()
    app.server_controller.is_running, app.server_controller.daemon_mode = True, True
    assert app.read_client_state() == ("connected", "running in a separate process")


def test_a_run_asked_to_stop_is_no_longer_active_while_its_thread_cleans_up():
    app = build_tray()
    make_running(app.server_controller, [("github", REGISTERED)], ready=False)
    app.server_controller.stop_event.set()
    assert app.read_client_state() == (None, "stopped")


@pytest.mark.parametrize("supervisor", [None, "previous run"])
def test_a_run_without_its_own_supervisor_yet_is_connecting(supervisor):
    app = build_tray()
    app.server_controller.is_running = True
    app.server_controller.supervisor = None if supervisor is None else MagicMock(cancelled=threading.Event(), link=MagicMock(ready=True))
    assert app.read_client_state() == ("connecting", "connecting...")


def test_a_link_that_is_not_registered_is_connecting():
    app = build_tray()
    make_running(app.server_controller, [("github", REGISTERED)], ready=False)
    assert app.read_client_state() == ("connecting", "connecting...")


def test_only_healthy_registered_servers_are_counted():
    app = build_tray()
    make_running(app.server_controller, [("github", REGISTERED), ("jira", DECLINED), ("slack", None)])
    app.server_controller.supervisor.registry.record_load(
        ServerLoad("broken", "stdio", failure=ServerFailure(COMMAND_NOT_FOUND, "not found", "Install it.")))
    assert app.read_client_state() == ("connected", "connected (1 server)")
    make_running(app.server_controller, [("github", REGISTERED), ("jira", REGISTERED)])
    assert app.read_client_state() == ("connected", "connected (2 servers)")


def test_a_session_with_no_registered_server_is_an_error():
    app = build_tray()
    make_running(app.server_controller, [("github", DECLINED)])
    assert app.read_client_state() == ("error", "error: connected, but no server is available (see Servers menu)")


def test_the_status_text_keeps_only_the_host_of_a_url_that_carries_a_key_in_its_path():
    text = fit_status_text(PREFIX, "error: cannot reach https://mcp.zapier.com/api/mcp/s/SECRETKEY0123456789/mcp: refused")
    assert "SECRETKEY" not in text
    assert "https://mcp.zapier.com" in text


def test_the_status_text_is_one_line():
    assert fit_status_text(PREFIX, "error: Config file has errors:\n  line 1\n\tline 2") == f"{PREFIX}error: Config file has errors: line 1 line 2"


@pytest.mark.parametrize("error", ["x" * 300, "🚀" * 200, "server 😀 " * 40])
def test_a_long_status_fits_the_windows_tooltip_with_its_prefix_and_suffix(error):
    text = fit_status_text(PREFIX, f"error: {error}", " — update 0.2.0 available")
    assert count_utf16_units(text) <= TOOLTIP_LIMIT
    assert text.startswith(f"{PREFIX}error: ")
    assert text.endswith("... — update 0.2.0 available")


def test_a_short_status_is_kept_whole():
    assert fit_status_text(PREFIX, "connected (3 servers)") == f"{PREFIX}connected (3 servers)"


def test_the_tooltip_names_an_update_only_when_the_tray_can_install_it():
    assert build_tray(can_update=True).format_tooltip("stopped") == f"{PREFIX}stopped — update 0.2.0 available"
    assert build_tray(can_update=False).format_tooltip("stopped") == f"{PREFIX}stopped"


def test_the_xorg_title_is_latin_1_so_python_xlib_can_write_it():
    app = build_tray(can_update=True)
    with patch.object(tray_module, "is_xorg_backend", return_value=True):
        title = app.format_tooltip("error: server 😀 failed")
    title.encode("latin-1")
    assert title == "ELITEA MCP Client - error: server ? failed - update 0.2.0 available"


def test_other_backends_keep_the_unicode_title():
    with patch.object(tray_module, "is_xorg_backend", return_value=False):
        assert build_tray().format_tooltip("error: server 😀 failed") == f"{PREFIX}error: server 😀 failed"


@pytest.mark.parametrize("system, header", [("Windows", f"{PREFIX}error: R&&D server failed"), ("Darwin", f"{PREFIX}error: R&D server failed")])
def test_the_menu_header_escapes_an_ampersand_only_where_windows_reads_it_as_a_shortcut(system, header):
    with patch.object(tray_module.platform, "system", return_value=system):
        assert build_tray().format_menu_header("error: R&D server failed") == header


def test_the_menu_header_shows_the_state():
    app = build_tray()
    app.server_controller.last_error = PLATFORM_ERROR
    assert app.get_menu_items()[0].text == f"{PREFIX}error: {PLATFORM_ERROR}"


def test_a_redraw_sets_the_tooltip_and_the_dot():
    app = build_tray()
    app.icon = MagicMock()
    app.create_icon_image = MagicMock()
    app.server_controller.last_error = PLATFORM_ERROR
    app.update_menu()
    assert app.icon.title == f"{PREFIX}error: {PLATFORM_ERROR}"
    app.create_icon_image.assert_called_once_with("error")


def test_a_new_config_error_is_announced_after_the_redraw_releases_its_lock():
    app = build_tray()
    app.icon = MagicMock()
    app.create_icon_image = MagicMock()
    lock_held = []
    app.report_config_error = MagicMock(side_effect=lambda: lock_held.append(app._redraw_lock.locked()))
    app._read_config = MagicMock(side_effect=lambda: setattr(app, "config_error", MagicMock(summary="broken")))
    app.update_menu()
    assert lock_held == [False]


def read_pixel(image, x, y):
    return image.getpixel((x, y))


@pytest.mark.parametrize("dot", ["error", "connecting", "connected"])
def test_the_dot_is_drawn_in_its_state_colour(dot):
    image = build_tray().create_icon_image(dot)
    assert read_pixel(image, image.width - 12, image.height - 12) == STATUS_DOT_COLORS[dot]


def test_a_stopped_client_draws_no_dot_and_the_badge_only_for_an_installable_update():
    plain = build_tray().create_icon_image(None)
    assert read_pixel(plain, plain.width - 12, plain.height - 12) not in STATUS_DOT_COLORS.values()
    assert read_pixel(plain, plain.width - 8, 8) != UPDATE_BADGE_COLOR
    badged = build_tray(can_update=True).create_icon_image(None)
    assert read_pixel(badged, badged.width - 8, 8) == UPDATE_BADGE_COLOR


@pytest.mark.parametrize("dark, shade", [(True, 255), (False, 0)])
def test_the_macos_logo_follows_the_menu_bar(dark, shade):
    app = build_tray()
    app.menu_bar_is_dark = dark
    with patch.object(tray_module.platform, "system", return_value="Darwin"):
        image = app.create_icon_image("connected")
    logo = {image.getpixel((x, y))[:3] for x in range(32) for y in range(32) if image.getpixel((x, y))[3]}
    assert logo == {(shade, shade, shade)}
    assert read_pixel(image, image.width - 12, image.height - 12) == STATUS_DOT_COLORS["connected"]


@pytest.mark.skipif(platform.system() != "Darwin", reason="the menu bar appearance exists only on macOS")
def test_a_pystray_without_its_status_item_keeps_the_white_logo():
    app = build_tray()
    app.icon = object()
    with patch.object(tray_module.AppKit, "NSOperationQueue") as queue:
        app.watch_menu_bar_appearance()
    queue.mainQueue.assert_not_called()
    assert app.menu_bar_is_dark is True


@pytest.fixture
def controller():
    """A controller whose run loop loads one healthy server, with the platform stubbed by each test."""
    controller = controller_module.ServerController()
    controller.config = {"servers": {"github": {}}, "timeout": 30}
    with patch.object(controller_module, "MONITOR_DELAY", 0.3), patch.object(controller_module, "SERVER_RESTART_DELAY", 0.05), \
            patch.object(ServerSupervisor, "load", lambda supervisor, names=None: supervisor.registry.record_load(
                ServerLoad("github", "stdio", tools=[]))):
        yield controller
    controller.stop_servers()


def slow_preflight(seconds, error):
    def preflight(config, notify):
        time.sleep(seconds)
        raise PreflightError(error)
    return preflight


def test_an_error_arriving_after_stop_is_notified_but_leaves_the_client_stopped(controller):
    app = build_tray(controller)
    notify_error = MagicMock()
    controller.set_error_callback(notify_error)
    with patch.object(controller_module, "validate_platform_config", side_effect=slow_preflight(1.0, PLATFORM_ERROR)):
        controller._start_embedded_servers()
        time.sleep(0.2)
        controller.stop_servers()
        wait_until(lambda: not controller.server_thread.is_alive())

    notify_error.assert_called_once_with(PLATFORM_ERROR)
    assert app.read_client_state() == (None, "stopped")


def test_an_error_arriving_while_stop_announces_itself_leaves_the_client_stopped(controller):
    app = build_tray(controller)
    controller.set_status_callback(lambda message: time.sleep(0.6) if message == "Stopping MCP servers..." else None)
    with patch.object(controller_module, "validate_platform_config", side_effect=slow_preflight(0.4, PLATFORM_ERROR)):
        controller._start_embedded_servers()
        time.sleep(0.1)
        controller.stop_servers()
        wait_until(lambda: not controller.server_thread.is_alive())
    assert app.read_client_state() == (None, "stopped")


def test_an_error_that_ends_a_run_turns_the_client_red(controller):
    app = build_tray(controller)
    with patch.object(controller_module, "validate_platform_config", side_effect=slow_preflight(0, PLATFORM_ERROR)):
        controller._start_embedded_servers()
        wait_until(lambda: not controller.server_thread.is_alive())
    assert app.read_client_state() == ("error", f"error: {PLATFORM_ERROR}")


def test_a_start_clears_the_last_error_and_never_stores_the_skipped_servers_warning(controller):
    controller.last_error = "old"
    report = MagicMock(config={"servers": {"github": {}}, "timeout": 30}, skipped_servers=["bad"])
    report.summarize_skipped_servers.return_value = "1 of 2 servers skipped."
    notify_error = MagicMock()
    controller.set_error_callback(notify_error)
    with patch.object(controller_module, "load_config", return_value={}), patch.object(controller_module, "check_config", return_value=report), \
            patch.object(controller_module, "write_config_schema"), patch.object(controller_module, "collect_skipped_servers", return_value=[]), \
            patch.object(controller_module, "validate_platform_config", side_effect=slow_preflight(5, PLATFORM_ERROR)):
        controller.start_servers()
        notify_error.assert_called_once_with("1 of 2 servers skipped.")
        assert controller.last_error is None


def test_a_refused_start_is_red_in_the_only_redraw_it_triggers(controller):
    app = build_tray(controller)
    report = MagicMock(config=None, summary="The config file has 1 problem.")
    seen = []
    controller.set_error_callback(lambda message: seen.append(app.read_client_state()))
    with patch.object(controller_module, "load_config", return_value={}), patch.object(controller_module, "check_config", return_value=report), \
            patch.object(controller_module, "write_config_schema"):
        assert controller.start_servers() is False
    assert seen == [("error", "error: The config file has 1 problem.")]


def register_every_server(config, registry, *callbacks):
    registry.mark_all_registered([entry.name for entry in registry.list_entries()])
    return MagicMock(ready=True)


def test_stopping_a_run_that_recorded_an_error_leaves_the_client_stopped(controller):
    app = build_tray(controller)
    with patch.object(controller_module, "validate_platform_config", side_effect=lambda config, notify: config), \
            patch.object(supervisor_module, "start_socket_connection", side_effect=register_every_server):
        controller._start_embedded_servers()
        wait_until(lambda: app.read_client_state()[0] == "connected")
        controller.supervisor.notify_error("Disconnected by the platform")
        controller.stop_servers()
    assert app.read_client_state() == (None, "stopped")


@pytest.fixture
def live(controller):
    """A tray on the real controller, supervisor and link, connected to a local socket.io platform; each status records the state the redraw reads."""
    port = find_free_port()
    sids = []
    platform_server = {"current": serve_platform(port, lambda sid, data: sids.append(sid))}
    controller.config = {"servers": {"github": {}}, "timeout": 30, "deployment_url": f"http://127.0.0.1:{port}",
                         "auth_token": "token", "project_id": 13}
    app = build_tray(controller)
    seen = []
    controller.set_status_callback(lambda message: seen.append((message, app.read_client_state())))
    controller.set_error_callback(lambda message: seen.append((message, app.read_client_state())))

    def restart_platform():
        drop_platform(*platform_server["current"])
        platform_server["current"] = serve_platform(port, lambda sid, data: sids.append(sid))

    with patch.object(controller_module, "validate_platform_config", side_effect=lambda config, notify: config), \
            patch.object(supervisor_module, "start_socket_connection", side_effect=connect_fast_reconnecting):
        yield MagicMock(app=app, seen=seen, sids=sids, server=lambda: platform_server["current"][1], restart_platform=restart_platform)
    controller.stop_servers()
    drop_platform(*platform_server["current"])


def connect_fast_reconnecting(*args):
    """The real link, reconnecting within a fraction of a second after a drop."""
    link = start_socket_connection(*args)
    link.sio.reconnection_delay, link.sio.reconnection_delay_max, link.sio.randomization_factor = 0.2, 0.3, 0
    return link


def states_after(seen, message):
    return [state for text, state in seen if text == message]


def test_live_first_connect_ends_connected(live):
    live.app.server_controller._start_embedded_servers()
    wait_until(lambda: live.app.read_client_state() == ("connected", "connected (1 server)"))
    assert live.seen[0] == ("Starting MCP servers...", ("connecting", "connecting..."))


def test_live_a_dropped_connection_is_connecting_until_the_reregistration_is_confirmed(live):
    live.app.server_controller._start_embedded_servers()
    wait_until(lambda: live.app.read_client_state()[0] == "connected")

    live.restart_platform()
    wait_until(lambda: len(states_after(live.seen, "Connected to platform")) == 2)

    assert states_after(live.seen, "Connection lost, reconnecting...") == [("connecting", "connecting...")]
    assert states_after(live.seen, "Connected to platform")[-1] == ("connected", "connected (1 server)")


def test_live_a_kick_by_the_platform_ends_red_with_its_reason(live):
    live.app.server_controller._start_embedded_servers()
    wait_until(lambda: live.app.read_client_state()[0] == "connected")

    live.server().disconnect(live.sids[0])
    wait_until(lambda: not live.app.server_controller.is_running)

    assert live.app.read_client_state() == ("error", "error: Disconnected by the platform")


def test_live_a_stop_never_passes_through_connecting(live):
    live.app.server_controller._start_embedded_servers()
    wait_until(lambda: live.app.read_client_state()[0] == "connected")
    stop_index = len(live.seen)

    live.app.server_controller.stop_servers()

    assert ("connecting", "connecting...") not in [state for _, state in live.seen[stop_index:]]
    assert live.app.read_client_state() == (None, "stopped")
