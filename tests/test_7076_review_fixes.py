import asyncio
import pathlib
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.elitea_mcp.utils import client_identity
from src.elitea_mcp.utils import server_controller as controller_module
from src.elitea_mcp.utils.session_manager import SessionManager

REPO = pathlib.Path(__file__).resolve().parents[1]


class SlowSession:
    def __init__(self):
        self.runs = 0

    async def call_tool(self, name, arguments):
        self.runs += 1
        await asyncio.sleep(2)


def test_a_slow_tool_on_a_stateful_server_runs_once_and_keeps_its_session():
    manager = SessionManager()
    manager.timeout = 0.3
    session = SlowSession()
    manager._sessions["browser"] = (MagicMock(), None, session)

    with patch.object(manager, "cleanup_session") as cleanup, pytest.raises(TimeoutError):
        asyncio.run(manager.call_tool_with_recovery("browser", {"stateful": True}, {"name": "t", "arguments": {}}))

    assert session.runs == 1
    cleanup.assert_not_called()
    assert "browser" in manager._sessions


@patch.object(controller_module, "validate_platform_config", side_effect=lambda config, notify: config)
def test_an_earlier_start_that_finishes_late_stops_only_its_own_session(mock_validate):
    release_first = threading.Event()
    supervisors = []

    def build_supervisor(*args, **kwargs):
        supervisor = MagicMock()
        supervisor.start.side_effect = (lambda: release_first.wait(5) and True) if not supervisors else (lambda: True)
        supervisors.append(supervisor)
        return supervisor

    controller = controller_module.ServerController()
    controller.config = {"servers": {"a": {}}}
    with patch.object(controller_module, "ServerSupervisor", side_effect=build_supervisor), \
            patch.object(controller_module, "MONITOR_DELAY", 0.1), patch.object(controller_module, "SERVER_RESTART_DELAY", 0.05):
        controller._start_embedded_servers()
        time.sleep(0.2)
        controller.stop_servers()
        controller._start_embedded_servers()
        time.sleep(0.2)
        release_first.set()
        time.sleep(0.3)
        controller.stop_servers()
        time.sleep(0.5)

    supervisors[0].stop.assert_called_once_with()
    supervisors[1].stop.assert_called_once_with()


def hold_client_id_in_another_process(config_dir):
    script = (
        "import pathlib, sys, time; sys.path.insert(0, 'src');"
        "from elitea_mcp.utils import client_identity;"
        f"client_identity.get_config_dir = lambda: pathlib.Path({str(config_dir)!r});"
        "print(client_identity.load_client_id(), flush=True); time.sleep(60)"
    )
    process = subprocess.Popen([sys.executable, "-c", script], cwd=REPO, stdout=subprocess.PIPE, text=True)
    return process, process.stdout.readline().strip()


def test_a_second_running_copy_gets_its_own_id_so_it_cannot_take_over_the_first(tmp_path):
    first, first_id = hold_client_id_in_another_process(tmp_path)
    try:
        second_id = client_identity.load_client_id()
    finally:
        first.kill()
        first.wait()

    assert first_id == (tmp_path / "client_id").read_text()
    assert second_id != first_id
    assert second_id.startswith(f"{client_identity.read_machine_part()}-")
    assert (tmp_path / "client_id").read_text() == first_id


def test_a_restart_after_the_first_copy_ended_reuses_its_id(tmp_path):
    first, first_id = hold_client_id_in_another_process(tmp_path)
    first.kill()
    first.wait()

    assert client_identity.load_client_id() == first_id


def test_the_machine_part_stays_the_same_when_no_mac_address_is_found():
    random_nodes = iter([0x6137FBF7B0E8, 0xC51D85BECBC4])

    with patch.object(client_identity.uuid, "getnode", side_effect=lambda: next(random_nodes)):
        assert client_identity.read_machine_part() == client_identity.read_machine_part()


@patch.object(controller_module, "validate_platform_config", side_effect=lambda config, notify: config)
def test_a_restart_during_a_slow_start_ends_the_old_run_and_keeps_the_new_one_running(mock_validate):
    release_first = threading.Event()
    supervisors = []

    def build_supervisor(*args, **kwargs):
        supervisor = MagicMock()
        supervisor.cancelled = kwargs["cancelled"]
        supervisor.start.side_effect = (lambda: release_first.wait(5) and True) if not supervisors else (lambda: True)
        supervisors.append(supervisor)
        return supervisor

    controller = controller_module.ServerController()
    controller.config = {"servers": {"a": {}}}
    with patch.object(controller_module, "ServerSupervisor", side_effect=build_supervisor), \
            patch.object(controller_module, "MONITOR_DELAY", 0.1), patch.object(controller_module, "SERVER_RESTART_DELAY", 0.05):
        controller._start_embedded_servers()
        time.sleep(0.2)
        old_thread = controller.server_thread
        controller.stop_servers()
        controller._start_embedded_servers()
        time.sleep(0.2)
        release_first.set()
        old_thread.join(2)

        assert not old_thread.is_alive()
        assert supervisors[0].cancelled.is_set() and not supervisors[1].cancelled.is_set()
        supervisors[0].stop.assert_called_once_with()
        assert controller.is_running is True
        controller.stop_servers()
