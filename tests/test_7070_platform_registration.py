import socket
import socketserver
import threading
import time
import wsgiref.simple_server
from unittest.mock import MagicMock, patch

import pytest
import socketio

from src.elitea_mcp.commands.run_servers import run_servers
from src.elitea_mcp.main import main
from src.elitea_mcp.utils import sio as sio_module
from src.elitea_mcp.utils.preflight import PreflightError
from src.elitea_mcp.utils.server_controller import ServerController
from src.elitea_mcp.utils.sio import PlatformConnectionError, start_socket_connection

TOOLS = [{"name": "github", "tools": []}]
WAIT_SECONDS = 7


class ThreadingWSGIServer(socketserver.ThreadingMixIn, wsgiref.simple_server.WSGIServer):
    daemon_threads = True


class QuietHandler(wsgiref.simple_server.WSGIRequestHandler):
    def log_message(self, *args):
        """Keep request logs out of the test output."""


def find_free_port():
    """Pick a local port the test platform can listen on."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def serve_platform(port, on_mcp_connect):
    """Run a socket.io server on the port that answers mcp_connect with the given handler."""
    server = socketio.Server(async_mode="threading")
    server.on("mcp_connect", on_mcp_connect)
    httpd = wsgiref.simple_server.make_server(
        "127.0.0.1", port, socketio.WSGIApp(server), server_class=ThreadingWSGIServer, handler_class=QuietHandler
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, server


def drop_platform(httpd, server):
    """Stop the server and cut its open connections, so connected clients see a dropped connection."""
    for engineio_socket in list(server.eio.sockets.values()):
        engineio_socket.close(wait=False, abort=True)
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def platform():
    """Run a socket.io server that answers mcp_connect with whatever the test sets as `platform.ack`."""
    state = MagicMock(ack=None, notification=None, payloads=[])
    server_holder = {}

    def mcp_connect(sid, data):
        state.payloads.append(data)
        if state.notification:
            server_holder["server"].emit("mcp_notification", state.notification, to=sid)
        return state.ack

    port = find_free_port()
    httpd, server_holder["server"] = serve_platform(port, mcp_connect)
    state.config = {"deployment_url": f"http://127.0.0.1:{port}", "auth_token": "token", "project_id": 13, "timeout": 30}
    yield state
    drop_platform(httpd, server_holder["server"])


def start_fast_reconnecting_client(config, notify_status, notify_error):
    """Connect a client that reconnects within a fraction of a second after a drop."""
    client = start_socket_connection(config, TOOLS, notify_status, notify_error)
    client.reconnection_delay = 0.2
    client.reconnection_delay_max = 0.3
    client.randomization_factor = 0
    return client


def count_calls(callback, message):
    """Count how often a notification callback received the message."""
    return [c.args[0] for c in callback.call_args_list].count(message)


def wait_until(condition):
    """Poll until the condition holds, failing the test after a few seconds."""
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.1)
    assert condition()


def test_connected_is_reported_only_after_the_platform_answers_the_registration(platform):
    notify_status, notify_error = MagicMock(), MagicMock()

    client = start_socket_connection(platform.config, TOOLS, notify_status, notify_error)

    assert platform.payloads[0]["project_id"] == 13
    notify_status.assert_called_once_with("Connected to platform")
    notify_error.assert_not_called()
    client.disconnect()


def test_refused_registration_raises_the_platform_message_and_disconnects(platform):
    platform.ack = {"ok": False, "error": "not_in_project", "message": "User has no access to project 13."}
    notify_status = MagicMock()

    with patch.object(socketio.Client, "disconnect", autospec=True, side_effect=socketio.Client.disconnect) as disconnect:
        with pytest.raises(PlatformConnectionError, match="User has no access to project 13."):
            start_socket_connection(platform.config, TOOLS, notify_status, MagicMock())

    disconnect.assert_called_once()
    assert "Connected to platform" not in [c.args[0] for c in notify_status.call_args_list]


def test_declined_server_is_reported_to_the_tray_with_its_reason(platform):
    platform.notification = "github - declined."
    notify_status = MagicMock()

    client = start_socket_connection(platform.config, TOOLS, notify_status, MagicMock())

    wait_until(lambda: notify_status.call_count == 2)
    notify_status.assert_any_call(
        'Server "github" was declined: a server with this name is already registered in project 13 '
        '(another running client, or a session that has not timed out yet).'
    )
    client.disconnect()


def test_stopping_the_client_is_a_status_not_an_error(platform):
    notify_status, notify_error = MagicMock(), MagicMock()
    client = start_socket_connection(platform.config, TOOLS, notify_status, notify_error)

    client.disconnect()

    wait_until(lambda: notify_status.call_count == 2)
    notify_status.assert_called_with("Disconnected from platform")
    notify_error.assert_not_called()


@patch.object(sio_module, "CONNECT_RETRY_DELAY", 0)
def test_unreachable_platform_fails_after_bounded_attempts():
    config = {"deployment_url": "http://127.0.0.1:9", "auth_token": "token", "project_id": 13, "timeout": 30}
    notify_status = MagicMock()

    with patch.object(socketio.Client, "connect", autospec=True, side_effect=socketio.exceptions.ConnectionError("refused")) as connect:
        with pytest.raises(PlatformConnectionError, match="Could not connect to http://127.0.0.1:9 after 3 attempts: refused"):
            start_socket_connection(config, TOOLS, notify_status, MagicMock())

    assert connect.call_count == 3
    assert all("retry" not in c.kwargs for c in connect.call_args_list)
    assert [c.args[0] for c in notify_status.call_args_list] == [
        "Cannot connect to platform (refused), retrying (attempt 2/3)...",
        "Cannot connect to platform (refused), retrying (attempt 3/3)...",
    ]


@patch("src.elitea_mcp.utils.server_controller.start_socket_connection")
@patch("src.elitea_mcp.utils.server_controller.get_all_tools")
@patch("src.elitea_mcp.utils.server_controller.validate_platform_config", side_effect=PreflightError("Authentication failed: the token is invalid or expired."))
@patch("src.elitea_mcp.utils.server_controller.load_config", return_value={"servers": {"github": {}}})
def test_tray_start_reports_the_preflight_failure_and_never_opens_a_socket(mock_load_config, mock_validate, mock_get_all_tools, mock_start_socket):
    controller = ServerController()
    controller.set_status_callback(MagicMock())
    controller.set_error_callback(MagicMock())

    controller.start_servers()
    controller.server_thread.join(timeout=WAIT_SECONDS)

    controller.error_callback.assert_called_once_with("Authentication failed: the token is invalid or expired.")
    mock_get_all_tools.assert_not_called()
    mock_start_socket.assert_not_called()
    assert controller.is_running is False


@patch("src.elitea_mcp.commands.run_servers.start_socket_connection")
@patch("src.elitea_mcp.commands.run_servers.validate_platform_config", side_effect=PreflightError('"project_id" is not set.'))
@patch("src.elitea_mcp.commands.run_servers.load_config", return_value={"servers": {"github": {}}})
def test_serve_exits_with_the_preflight_message(mock_load_config, mock_validate, mock_start_socket, capsys):
    with pytest.raises(SystemExit) as exit_info:
        run_servers()

    assert exit_info.value.code == 1
    assert 'Cannot connect to platform: "project_id" is not set.' in capsys.readouterr().out
    mock_start_socket.assert_not_called()


@patch("src.elitea_mcp.main.logger")
@patch("src.elitea_mcp.main.Agents")
@patch("src.elitea_mcp.main.validate_platform_config", side_effect=PreflightError("Your token has no access to project 13."))
@patch("src.elitea_mcp.main.get_bootstrap_config", return_value={"deployment_url": "https://elitea.example.com", "auth_token": "token"})
def test_run_logs_the_preflight_message_and_never_fetches_agents(mock_config, mock_validate, mock_agents, mock_logger):
    main(project_id="13")

    mock_logger.error.assert_called_once_with("Your token has no access to project 13.")
    mock_agents.assert_not_called()


@patch.object(sio_module, "REGISTRATION_TIMEOUT", 2)
def test_late_timeout_from_a_superseded_reconnect_keeps_the_newer_connection():
    port = find_free_port()
    config = {"deployment_url": f"http://127.0.0.1:{port}", "auth_token": "token", "project_id": 13, "timeout": 30}
    notify_status, notify_error = MagicMock(), MagicMock()
    first_platform = serve_platform(port, lambda sid, data: None)
    client = start_fast_reconnecting_client(config, notify_status, notify_error)

    drop_platform(*first_platform)
    wait_until(lambda: count_calls(notify_status, "Connection lost, reconnecting...") == 1)
    unanswered = threading.Event()
    silent_platform = serve_platform(port, lambda sid, data: unanswered.set() or time.sleep(10))
    wait_until(unanswered.is_set)
    drop_platform(*silent_platform)
    wait_until(lambda: count_calls(notify_status, "Connection lost, reconnecting...") == 2)
    answering_platform = serve_platform(port, lambda sid, data: None)
    wait_until(lambda: count_calls(notify_status, "Connected to platform") == 2)

    time.sleep(sio_module.REGISTRATION_TIMEOUT + 1)

    notify_error.assert_not_called()
    assert client.connected is True
    client.disconnect()
    drop_platform(*answering_platform)


def test_refused_registration_after_a_reconnect_is_reported_and_disconnects():
    port = find_free_port()
    config = {"deployment_url": f"http://127.0.0.1:{port}", "auth_token": "token", "project_id": 13, "timeout": 30}
    notify_status, notify_error = MagicMock(), MagicMock()
    first_platform = serve_platform(port, lambda sid, data: None)
    client = start_fast_reconnecting_client(config, notify_status, notify_error)

    drop_platform(*first_platform)
    refusing_platform = serve_platform(port, lambda sid, data: {"ok": False, "message": "User has no access to project 13."})

    wait_until(lambda: notify_error.call_count == 1)
    notify_error.assert_called_once_with("User has no access to project 13.")
    wait_until(lambda: client.connected is False)
    drop_platform(*refusing_platform)
