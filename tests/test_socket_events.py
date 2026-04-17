import threading
import time
from unittest.mock import call, patch, MagicMock

import socketio

from src.elitea_mcp.utils.server_controller import ServerController

connect_event = threading.Event()
disconnect_event = threading.Event()
stop_event = threading.Event()
thread = None
interaction_timeout = 7  # common wait timeout in seconds


def create_socket_server(async_mode='threading'):
    sio_server = socketio.Server(async_mode=async_mode)
    app = socketio.WSGIApp(sio_server)

    @sio_server.event
    def connect(sid, environ):
        connect_event.set()
        pass

    @sio_server.event
    def disconnect(sid, environ):
        disconnect_event.set()
        pass

    return sio_server, app


def run_server():
    import wsgiref.simple_server
    sio_server, app = create_socket_server()
    server = wsgiref.simple_server.make_server('localhost', 5002, app)
    server.allow_reuse_address = True
    
    while not stop_event.is_set():
        server.handle_request()
    
    server.server_close()


def start_server():
    global thread, stop_event
    stop_event.clear()
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    time.sleep(1)


def stop_server():
    global thread
    stop_event.set()
    thread.join()
    time.sleep(1)
    stop_event.clear()


@patch("src.elitea_mcp.utils.server_controller.get_all_tools")
@patch("src.elitea_mcp.utils.server_controller.load_config")
def test_events_for_start_servers(mock_load_config, mock_get_all_tools):
    start_server()
    mock_load_config.return_value = {
        "deployment_url": "http://localhost:5002", "auth_token": "any_token", "host": "0.0.0.0", "port": 8000, "servers": {"mcp_1": {"name": "mcp_server_1"}}, "project_id": 123
    }
    mock_get_all_tools.return_value = [{
        'name': 'mcp_server_1', 'tools': [{'annotations': None, 'description': 'descr', 'inputSchema': {'properties': {'task': {}}, 'required': ['task'], 'type': 'object'}, 'meta': None, 'name': 'ant_te_st-__2345', 'outputSchema': None, 'title': None}]
    }]
    mock_status_callback = MagicMock()
    mock_error_callback = MagicMock()
    controller = ServerController()
    controller.is_running = False
    controller.daemon_mode = False
    controller.status_callback = mock_status_callback
    controller.error_callback = mock_error_callback
    
    result = controller.start_servers()
    
    connect_event.wait(timeout=interaction_timeout)  # Wait for client connected to server socket
    connect_event.clear()
    wait_for_condition(lambda: mock_status_callback.call_count == 4)
    
    expected_statuses = [
        call.__bool__(),
        call("Starting MCP servers..."),
        call.__bool__(),
        call("Connected to servers: mcp_server_1"),
        call.__bool__(),
        call("Starting socket connection..."),
        call.__bool__(),
        call("Connected to platform"),
    ]
    mock_status_callback.assert_has_calls(expected_statuses) # ensure all expected status calls were made
    mock_error_callback.assert_not_called() # ensure no error calls were made
    assert controller.is_running is True
    assert result is True
    
    stop_server() # Stop the server to simulate disconnection or restart

    wait_for_condition(lambda: mock_error_callback.call_count == 1) # Wait for exectly one error callback to be called due to disconnection

    expected_errors = [
        call.__bool__(),
        call("Disconnected from platform"),
    ]
    mock_error_callback.assert_has_calls(expected_errors)
    mock_status_callback.assert_has_calls(expected_statuses)
    assert mock_status_callback.call_count == 4
    
    start_server() # Restart the server to simulate reconnection and verify socket client recovery

    connect_event.wait(timeout=interaction_timeout)  # Wait for client reconnected to server socket
    wait_for_condition(lambda: mock_status_callback.call_count == 4)
    
    mock_status_callback.assert_has_calls(expected_statuses) # ensure new reconnection status calls were made
    assert mock_error_callback.call_count == 1 # ensure number of error calls remains the same
    assert controller.is_running is True


def wait_for_condition(condition_fn, timeout=interaction_timeout, poll_interval=0.5):
    """
    Waits until condition_fn() returns True or raises AssertionError on timeout.
    """
    start = time.time()
    while time.time() - start < timeout:
        if condition_fn():
            return
        time.sleep(poll_interval)
    assert condition_fn(), f"Condition was not met within {timeout} seconds."
