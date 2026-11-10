from unittest.mock import MagicMock, patch

import pytest

from src.elitea_mcp import tray as tray_module
from src.elitea_mcp.utils.server_loader import COMMAND_NOT_FOUND, ServerFailure, ServerLoad
from src.elitea_mcp.utils.server_registry import ServerRegistry


def build_registry():
    registry = ServerRegistry()
    registry.record_load(ServerLoad("jira", "stdio", tools=[{"name": f"t{number}"} for number in range(14)]))
    registry.record_load(ServerLoad("github", "stdio", tools=[]))
    registry.record_load(ServerLoad("confluence", "stdio", failure=ServerFailure(COMMAND_NOT_FOUND, 'command "uvx" not found', "Install uv.")))
    registry.mark_all_registered(["jira"])
    registry.apply_registration(["github"], set(), [{"name": "github", "reason": "name_already_registered"}])
    return registry


@pytest.fixture
def app(tmp_path):
    controller = MagicMock(daemon_mode=False, is_running=True)
    controller.list_servers.return_value = build_registry().list_entries()
    with patch.object(tray_module, "get_server_controller", return_value=controller), patch.object(tray_module, "UpdateChecker"):
        app = tray_module.MCPTrayApp()
    app._open_file = MagicMock()
    app.show_notification = MagicMock()
    return app


def find_servers_menu(app):
    return next(item for item in app.get_menu_items() if item.text == "Servers").submenu


def test_the_servers_menu_shows_one_row_per_server_and_a_retry_item(app):
    texts = [item.text for item in find_servers_menu(app).items]

    assert texts == ["✓ jira — 14 tools, registered", "⚠ github — declined (name already registered)",
                     '✗ confluence — failed: command "uvx" not found', "- - - -", "Retry Failed Servers"]
    assert find_servers_menu(app).items[-1].enabled is True


def test_retry_failed_servers_asks_the_controller_to_reload_the_failed_ones(app):
    find_servers_menu(app).items[-1](MagicMock())

    app.server_controller.retry_failed_servers.assert_called_once_with()


def test_clicking_a_failed_row_writes_and_opens_its_error_file(app, tmp_path):
    app.server_controller.format_server_error.return_value = "Server details\n"

    with patch.object(tray_module, "get_default_paths", return_value={"tray_log": tmp_path / "elitea-mcp-tray.log"}):
        find_servers_menu(app).items[2](MagicMock())

    app.server_controller.format_server_error.assert_called_once_with("confluence")
    assert (tmp_path / "confluence-error.txt").read_text() == "Server details\n"
    app._open_file.assert_called_once_with(tmp_path / "confluence-error.txt")


def test_clicking_a_healthy_row_opens_nothing(app):
    find_servers_menu(app).items[0](MagicMock())

    app._open_file.assert_not_called()


def test_there_is_no_servers_menu_before_the_first_start(app):
    app.server_controller.list_servers.return_value = []

    assert "Servers" not in [item.text for item in app.get_menu_items()]


def test_a_server_message_is_a_notification_and_never_marks_the_client_stopped(app):
    app.is_server_running = True

    app.on_server_message('Server "github": command "npx" not found.')

    app.show_notification.assert_called_once_with("MCP Servers", 'Server "github": command "npx" not found.')
    assert app.is_server_running is True
