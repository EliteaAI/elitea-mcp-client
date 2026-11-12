import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.elitea_mcp import tray as tray_module
from src.elitea_mcp.commands import run_servers as run_servers_module
from src.elitea_mcp.utils import server_controller as controller_module
from src.elitea_mcp.utils import server_supervisor as supervisor_module
from src.elitea_mcp.utils import sio as sio_module
from src.elitea_mcp.utils.diagnostics import DOCTOR_HINT
from src.elitea_mcp.utils.server_loader import COMMAND_NOT_FOUND, ServerFailure, ServerLoad
from src.elitea_mcp.utils.server_supervisor import ServerSupervisor
from src.elitea_mcp.utils.session_manager import SessionManager
from src.elitea_mcp.utils.sio import start_socket_connection
from tests.test_7070_platform_registration import create_registry, drop_platform, find_free_port, serve_platform, wait_until

STOPPED = "MCP servers stopped"
RUN_CONTROLS = ["Stop MCP Server", "Restart MCP Server"]


def platform_config(port):
    return {"deployment_url": f"http://127.0.0.1:{port}", "auth_token": "token", "project_id": 13, "timeout": 30}


def serve_recording_platform(port, ack=None):
    """Run a socket.io platform that keeps the sid of every client that registers, so a test can disconnect it."""
    sids = []
    httpd, server = serve_platform(port, lambda sid, data: sids.append(sid) or ack)
    return httpd, server, sids


def connect_fast_reconnecting_link(config, notify_status, notify_error, ended):
    """Connect a link that reconnects within a fraction of a second after a drop."""
    link = start_socket_connection(config, create_registry(), notify_status, notify_error, None, ended)
    link.sio.reconnection_delay = 0.2
    link.sio.reconnection_delay_max = 0.3
    link.sio.randomization_factor = 0
    return link


def test_a_disconnect_by_the_platform_ends_the_session_once_and_never_reconnects():
    port = find_free_port()
    httpd, server, sids = serve_recording_platform(port)
    notify_error, ended = MagicMock(), MagicMock()
    link = connect_fast_reconnecting_link(platform_config(port), MagicMock(), notify_error, ended)

    server.disconnect(sids[0])
    wait_until(lambda: ended.called)
    time.sleep(1)

    ended.assert_called_once_with()
    notify_error.assert_called_once_with("Disconnected by the platform")
    assert link.connected is False
    assert len(sids) == 1
    drop_platform(httpd, server)


class SlowTeardown:
    """The context of a stateful session whose teardown takes a while, as a real server process does."""

    def __init__(self):
        self.closed = threading.Event()

    async def __aexit__(self, *args):
        await asyncio.sleep(0.5)
        self.closed.set()


def test_a_disconnect_by_the_platform_ends_the_session_only_after_stateful_sessions_are_torn_down():
    port = find_free_port()
    httpd, server, sids = serve_recording_platform(port)
    manager = SessionManager()
    manager._ensure_event_loop()
    teardown = SlowTeardown()
    manager._sessions["browser"] = (teardown, None, MagicMock())
    stopped = threading.Event()

    with patch.object(sio_module, "get_session_manager", return_value=manager):
        link = connect_fast_reconnecting_link(platform_config(port), MagicMock(), MagicMock(), None)
        stop_like_the_run_does = lambda: link.on_disconnect(link.sio.reason.CLIENT_DISCONNECT) or stopped.set()
        link.on_session_ended = lambda: threading.Thread(target=stop_like_the_run_does, daemon=True).start()
        server.disconnect(sids[0])
        wait_until(stopped.is_set)

    assert teardown.closed.is_set()
    assert not manager._lock.locked()
    drop_platform(httpd, server)


def test_a_dropped_connection_reconnects_and_never_ends_the_session():
    port = find_free_port()
    first = serve_recording_platform(port)
    notify_status, ended = MagicMock(), MagicMock()
    link = connect_fast_reconnecting_link(platform_config(port), notify_status, MagicMock(), ended)

    drop_platform(*first[:2])
    second = serve_recording_platform(port)
    wait_until(lambda: [c.args[0] for c in notify_status.call_args_list].count("Connected to platform") == 2)

    ended.assert_not_called()
    assert link.connected is True
    link.disconnect()
    drop_platform(*second[:2])


def test_a_refused_registration_after_a_reconnect_ends_the_session():
    port = find_free_port()
    first = serve_recording_platform(port)
    notify_error, ended = MagicMock(), MagicMock()
    link = connect_fast_reconnecting_link(platform_config(port), MagicMock(), notify_error, ended)

    drop_platform(*first[:2])
    refusing = serve_recording_platform(port, ack={"ok": False, "message": "User has no access to project 13."})
    wait_until(lambda: ended.called)

    ended.assert_called_once_with()
    notify_error.assert_called_once_with("User has no access to project 13.")
    assert link.connected is False
    drop_platform(*refusing[:2])


def test_stopping_the_link_from_this_client_never_ends_the_session():
    port = find_free_port()
    httpd, server, _ = serve_recording_platform(port)
    ended = MagicMock()
    link = connect_fast_reconnecting_link(platform_config(port), MagicMock(), MagicMock(), ended)

    link.disconnect()
    wait_until(lambda: link.connected is False)
    time.sleep(0.5)

    ended.assert_not_called()
    drop_platform(httpd, server)


def load_one_healthy_server(supervisor, names=None):
    supervisor.registry.record_load(ServerLoad("github", "stdio", tools=[]))


def load_one_broken_server(supervisor, names=None):
    supervisor.registry.record_load(ServerLoad("github", "stdio", failure=ServerFailure(COMMAND_NOT_FOUND, "not found", "Install it.")))


@pytest.fixture
def session():
    """Run the controller's embedded loop against a stand-in platform, handing back the callback a link calls when the platform ends the session."""
    state = MagicMock(link=MagicMock(connected=True), end_session=None)

    def connect(config, registry, notify_status, notify_error, on_server_failed, on_session_ended):
        registry.mark_all_registered(["github"])
        state.end_session = on_session_ended
        return state.link

    controller = controller_module.ServerController()
    controller.config = {"servers": {"github": {}}, "timeout": 30}
    state.controller = controller
    with patch.object(controller_module, "validate_platform_config", side_effect=lambda config, notify: config), \
            patch.object(controller_module, "SERVER_RESTART_DELAY", 0.05), patch.object(controller_module, "MONITOR_DELAY", 0.5), \
            patch.object(supervisor_module, "start_socket_connection", side_effect=connect), \
            patch.object(ServerSupervisor, "load", load_one_healthy_server):
        yield state
    controller.stop_servers()


def statuses(callback):
    return [c.args[0] for c in callback.call_args_list]


def test_a_session_the_platform_ends_stops_the_run_without_a_second_stopped_message(session):
    notify_status, notify_error = MagicMock(), MagicMock()
    session.controller.set_status_callback(notify_status)
    session.controller.set_error_callback(notify_error)
    session.controller._start_embedded_servers()
    wait_until(lambda: session.end_session is not None)

    session.controller._notify_error("Disconnected by the platform")
    session.end_session()
    wait_until(lambda: not session.controller.is_running)

    session.link.disconnect.assert_called_once_with()
    notify_error.assert_called_once_with("Disconnected by the platform")
    assert STOPPED not in statuses(notify_status)
    assert statuses(notify_status)[-1] == ""


def test_stopping_a_healthy_run_still_reports_it_stopped(session):
    notify_status = MagicMock()
    session.controller.set_status_callback(notify_status)
    session.controller._start_embedded_servers()
    wait_until(lambda: session.end_session is not None)

    session.controller.stop_servers()

    assert statuses(notify_status)[-1] == STOPPED
    assert "" not in statuses(notify_status)


def test_a_start_with_no_healthy_server_reports_the_error_alone_and_still_redraws_the_tray(session):
    notify_status, notify_error = MagicMock(), MagicMock()
    session.controller.set_status_callback(notify_status)
    session.controller.set_error_callback(notify_error)
    with patch.object(ServerSupervisor, "load", load_one_broken_server):
        session.controller._start_embedded_servers()
        wait_until(lambda: not session.controller.server_thread.is_alive())

    assert notify_error.call_count == 1
    assert STOPPED not in statuses(notify_status)
    assert statuses(notify_status)[-1] == ""
    assert session.controller.is_running is False


@pytest.fixture
def tray(session):
    """A tray app driving the real controller, with its config and notifications stubbed."""
    with patch.object(tray_module, "get_server_controller", return_value=session.controller), \
            patch.object(tray_module, "UpdateChecker"):
        app = tray_module.MCPTrayApp()
    app._read_config = MagicMock(return_value={"deployment_url": "http://127.0.0.1:9", "auth_token": "token"})
    app.config_report = MagicMock(notable=[])
    app.show_notification = MagicMock()
    app.show_error = MagicMock()
    app.icon = MagicMock()
    return app


def run_controls(app):
    return [item.text for item in app.get_menu_items() if item.text in RUN_CONTROLS + ["Start MCP Server"]]


def test_an_error_from_a_live_session_keeps_the_stop_and_restart_controls(tray, session):
    session.controller.config = {"servers": {"github": {}}, "timeout": 30}
    session.controller._start_embedded_servers()
    wait_until(lambda: session.end_session is not None)

    session.controller._notify_error("Could not update the platform")

    assert tray.is_server_running is True
    assert run_controls(tray) == RUN_CONTROLS


def test_start_works_again_once_the_platform_ended_the_session(tray, session):
    session.controller.config = {"servers": {"github": {}}, "timeout": 30}
    session.controller._start_embedded_servers()
    wait_until(lambda: session.end_session is not None)
    first_end_session = session.end_session

    session.controller._notify_error("Disconnected by the platform")
    first_end_session()
    wait_until(lambda: not session.controller.is_running)
    assert run_controls(tray) == ["Start MCP Server"]

    with patch.object(controller_module, "load_config", return_value={**platform_config(9), "servers": {"github": {"command": "npx"}}}), \
            patch.object(controller_module, "write_config_schema"):
        tray.start_server()
    wait_until(lambda: session.end_session is not first_end_session)

    assert session.controller.is_running is True
    assert run_controls(tray) == RUN_CONTROLS


def test_serve_exits_with_an_error_when_the_platform_ends_the_session(monkeypatch, capsys):
    monkeypatch.setattr(run_servers_module, "load_config", lambda: {"servers": {"github": {"command": "npx"}}})
    monkeypatch.setattr(run_servers_module, "write_config_schema", MagicMock())
    monkeypatch.setattr(run_servers_module, "validate_platform_config", lambda config: config)
    monkeypatch.setattr(run_servers_module, "print_server_table", MagicMock())
    supervisor = MagicMock(cancelled=threading.Event())
    supervisor.start.side_effect = lambda: threading.Timer(0.2, supervisor.cancelled.set).start() or True
    monkeypatch.setattr(run_servers_module, "ServerSupervisor", MagicMock(return_value=supervisor))

    with pytest.raises(SystemExit) as exit_info:
        run_servers_module.run_servers()

    assert exit_info.value.code == 1
    supervisor.stop.assert_called_once_with()
    assert capsys.readouterr().out.endswith(f"{DOCTOR_HINT}\n")
