from src.elitea_mcp.utils.server_controller import ServerController
from unittest.mock import patch, MagicMock


def test_restart_servers_when_running():
    controller = ServerController()
    controller.is_running = True
    controller.daemon_mode = True
    with patch.object(controller, "stop_servers") as stop_mock, \
         patch.object(controller, "start_servers", return_value=True) as start_mock, \
         patch("src.elitea_mcp.utils.server_controller.time.sleep"):
        result = controller.restart_servers()
        stop_mock.assert_called_once()
        start_mock.assert_called_once_with(daemon_mode=True)
        assert result is True


def test_notify_status_with_callback():
    """Test that _notify_status calls the status_callback when set."""
    controller = ServerController()
    
    # Create a mock callback
    mock_callback = MagicMock()
    controller.status_callback = mock_callback
    
    # Call the method
    test_message = "Test status message"
    controller._notify_status(test_message)
    
    # Verify callback was called with correct message
    mock_callback.assert_called_once_with(test_message)


def test_restart_servers_when_not_running():
    controller = ServerController()
    controller.is_running = False
    controller.daemon_mode = False
    with patch.object(controller, "stop_servers") as stop_mock, \
         patch.object(controller, "start_servers", return_value=True) as start_mock, \
         patch("src.elitea_mcp.utils.server_controller.time.sleep"):
        result = controller.restart_servers()
        stop_mock.assert_not_called()
        start_mock.assert_called_once_with(daemon_mode=False)
        assert result is True


def test_notify_status_without_callback():
    """Test that _notify_status doesn't crash when callback is not set."""
    controller = ServerController()
    controller.status_callback = None
    
    # This should not raise an exception
    controller._notify_status("This should not crash")
    
    # No assertion needed - test passes if no exception is raised


def test_notify_error_with_callback():
    """Test that _notify_error calls the error_callback when set."""
    controller = ServerController()
    
    # Create a mock callback
    mock_callback = MagicMock()
    controller.error_callback = mock_callback
    
    # Call the method
    test_message = "Test error message"
    controller._notify_error(test_message)
    
    # Verify callback was called with correct message
    mock_callback.assert_called_once_with(test_message)


def test_notify_error_without_callback():
    """Test that _notify_error doesn't crash when callback is not set."""
    controller = ServerController()
    controller.error_callback = None
    
    # This should not raise an exception
    controller._notify_error("This should not crash")
    
    # No assertion needed - test passes if no exception is raised
