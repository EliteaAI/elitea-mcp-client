import platform
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.elitea_mcp import config as config_module
from src.elitea_mcp import tray as tray_module
from src.elitea_mcp.utils import package_updates
from src.elitea_mcp.utils.package_updates import UpdateChecker, UpdateState, fetch_update_state

PYPI_INFO = {"info": {"version": "0.1.37", "requires_python": ">=3.10"}}


def pypi_response(payload=PYPI_INFO, status=200):
    """Build a fake PyPI JSON response."""
    response = MagicMock(status_code=status)
    response.json.return_value = payload
    response.raise_for_status.side_effect = (
        requests.exceptions.HTTPError(response=response) if status >= 400 else None
    )
    return response


@pytest.fixture
def installed(monkeypatch):
    """Pin the installed version the checker compares against."""
    monkeypatch.setattr(package_updates, "get_installed_version", lambda: "0.1.36")


def test_update_available_names_both_versions(installed):
    with patch.object(package_updates.requests, "get", return_value=pypi_response()):
        state = fetch_update_state()
    assert state.can_update
    assert state.describe() == "Version 0.1.37 is available. You have 0.1.36."


def test_up_to_date_is_reported_as_latest(installed):
    payload = {"info": {"version": "0.1.36", "requires_python": ">=3.10"}}
    with patch.object(package_updates.requests, "get", return_value=pypi_response(payload)):
        state = fetch_update_state()
    assert not state.can_update
    assert state.describe() == "You are on the latest version (0.1.36)."


@pytest.mark.parametrize("error, reason", [
    (requests.exceptions.ConnectTimeout(), "pypi.org did not respond"),
    (requests.exceptions.ReadTimeout(), "pypi.org did not respond"),
    (requests.exceptions.SSLError(), "TLS verification failed for pypi.org (corporate proxy?)"),
    (requests.exceptions.ConnectionError(), "cannot reach pypi.org (no network or DNS failure)"),
])
def test_failed_check_is_distinct_from_up_to_date(installed, error, reason):
    with patch.object(package_updates.requests, "get", side_effect=error):
        state = fetch_update_state()
    assert state.describe() == f"Could not check for updates: {reason}."
    assert not state.can_update


def test_http_error_reports_the_status(installed):
    with patch.object(package_updates.requests, "get", return_value=pypi_response(status=503)):
        state = fetch_update_state()
    assert state.error == "PyPI returned HTTP 503"


def test_unparseable_response_is_a_failed_check(installed):
    with patch.object(package_updates.requests, "get", return_value=pypi_response(payload={})):
        state = fetch_update_state()
    assert state.error == "PyPI returned an unexpected response"


def test_release_requiring_newer_python_cannot_be_installed(installed):
    payload = {"info": {"version": "0.1.37", "requires_python": ">=99"}}
    with patch.object(package_updates.requests, "get", return_value=pypi_response(payload)):
        state = fetch_update_state()
    assert state.is_update_available and not state.can_update
    assert state.describe() == f"Version 0.1.37 requires Python >=99; you have {platform.python_version()}."


def test_periodic_check_announces_only_changes():
    found, same_again, failed = UpdateState("0.1.36", "0.1.37"), UpdateState("0.1.36", "0.1.37"), UpdateState("0.1.36", error="x")
    pending = [found, same_again, failed]
    announced = []
    checker = UpdateChecker(announced.append, interval_hours=1e-9)

    def fetch_next():
        state = pending.pop(0)
        if not pending:
            checker.stop()
        return state

    with patch.object(package_updates, "fetch_update_state", side_effect=fetch_next):
        checker._check_periodically()
    assert announced == [found, failed]


@pytest.mark.parametrize("interval_hours", [0, -1])
def test_non_positive_interval_checks_once(interval_hours):
    checker = UpdateChecker(MagicMock(), interval_hours=interval_hours)

    def fetch_and_stop_a_spinning_loop():
        if fetch.call_count >= 3:
            checker.stop()
        return UpdateState("0.1.36")

    with patch.object(package_updates, "fetch_update_state", side_effect=fetch_and_stop_a_spinning_loop) as fetch:
        checker._check_periodically()
    assert fetch.call_count == 1


class InlineThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        """Run the target immediately so the test sees its effect."""
        self.target()


def test_manual_check_reports_the_outcome_once():
    app, _ = build_tray()
    app.show_notification = MagicMock()
    app.update_menu = MagicMock()
    with patch.object(tray_module.threading, "Thread", InlineThread), \
            patch.object(package_updates, "fetch_update_state", return_value=UpdateState("0.1.36", "0.1.37")):
        app.check_for_updates()
    app.show_notification.assert_called_once_with("Check for Updates", "Version 0.1.37 is available. You have 0.1.36.")
    app.update_menu.assert_called_once()


def test_update_check_config_defaults_on(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "get_config_file", lambda: tmp_path / "config.json")
    loaded = config_module.get_bootstrap_config()
    assert loaded["update_check"] is True
    assert loaded["update_check_interval_hours"] == 24


def build_tray(update_check=True, state=None):
    """Create a tray app without touching the user's config or real servers."""
    bootstrap = {"deployment_url": "https://elitea.example.com", "auth_token": "token",
                 "update_check": update_check, "update_check_interval_hours": 24}
    with patch.object(tray_module, "get_bootstrap_config", return_value=bootstrap), \
            patch.object(tray_module, "get_server_controller", return_value=MagicMock(daemon_mode=False)):
        app = tray_module.MCPTrayApp()
    if state is not None:
        app.update_checker.state = state
    return app, bootstrap


def menu_texts(app, bootstrap):
    """Render the menu and return the item labels."""
    with patch.object(tray_module, "get_bootstrap_config", return_value=bootstrap):
        return [item.text for item in app.get_menu_items()]


def test_menu_build_never_calls_the_network():
    app, bootstrap = build_tray(state=UpdateState("0.1.36", "0.1.37"))
    with patch.object(package_updates.requests, "get", side_effect=AssertionError("network call")) as get:
        app.icon = MagicMock()
        app.on_server_status_change("Starting MCP servers...")
        app.on_server_error("boom")
        menu_texts(app, bootstrap)
    assert get.call_count == 0


def test_update_items_follow_the_cached_state():
    app, bootstrap = build_tray(state=UpdateState("0.1.36", "0.1.37"))
    assert "Update to 0.1.37 and Restart" in menu_texts(app, bootstrap)
    assert "Release Notes for 0.1.37" in menu_texts(app, bootstrap)
    app.update_checker.state = UpdateState("0.1.37", "0.1.37")
    texts = menu_texts(app, bootstrap)
    assert "Check for Updates" in texts
    assert not any(text.startswith("Update to") for text in texts)


def test_disabled_update_check_hides_update_items():
    app, bootstrap = build_tray(update_check=False)
    assert app.update_checker is None
    texts = menu_texts(app, bootstrap)
    assert "Check for Updates" not in texts
    assert not any(text.startswith("Update to") for text in texts)


def test_about_shows_the_installed_version():
    app, _ = build_tray()
    app.show_notification = MagicMock()
    with patch.object(tray_module, "get_installed_version", return_value="0.1.36"):
        app.show_about()
    assert "Version: 0.1.36" in app.show_notification.call_args.args[1]
    with patch.object(tray_module, "get_installed_version", return_value=None):
        app.show_about()
    assert "0.1.16" not in app.show_notification.call_args.args[1]
