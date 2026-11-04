import json
import os
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.elitea_mcp import tray as tray_module
from src.elitea_mcp.utils import self_update, update_helper
from src.elitea_mcp.utils.package_updates import UpdateState
from src.elitea_mcp.utils.self_update import UpgradePlan, consume_update_result, resolve_upgrade_plan

EXECUTABLE = "/envs/elitea/bin/python"
HELPER_SCRIPT = Path(update_helper.__file__)


class FakeDistribution:
    def __init__(self, site_packages, direct_url=None):
        self.site_packages = site_packages
        self.direct_url = direct_url

    def read_text(self, name):
        """Return the dist-info file the resolver asks for."""
        return json.dumps(self.direct_url) if name == "direct_url.json" and self.direct_url else None

    def locate_file(self, path):
        """Return the site-packages dir the package lives in."""
        return self.site_packages


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Provide a fake install prefix whose distribution and tools the test controls."""
    prefix = tmp_path / "prefix"
    site_packages = prefix / "site-packages"
    site_packages.mkdir(parents=True)
    distribution = FakeDistribution(site_packages)
    monkeypatch.setattr(self_update.importlib.metadata, "distribution", lambda name: distribution)
    monkeypatch.setattr(self_update.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(self_update, "is_pip_available", lambda: True)
    return Namespace(prefix=prefix, distribution=distribution, site_packages=site_packages)


def resolve_in_venv(env):
    """Resolve the plan as if running inside the fake prefix's venv."""
    return resolve_upgrade_plan(prefix=str(env.prefix), base_prefix="/base", executable=EXECUTABLE)


def test_pipx_install_is_detected_by_its_marker_file(env):
    (env.prefix / "pipx_metadata.json").write_text("{}")
    assert resolve_in_venv(env) == UpgradePlan(["/usr/bin/pipx", "upgrade", "elitea-mcp"], can_run=True)


def test_uv_tool_install_reinstalls_latest_to_escape_a_version_pin(env):
    (env.prefix / "uv-receipt.toml").write_text("")
    assert resolve_in_venv(env) == UpgradePlan(["/usr/bin/uv", "tool", "install", "elitea-mcp@latest"], can_run=True)


def test_venv_with_pip_upgrades_through_its_own_interpreter(env):
    plan = resolve_in_venv(env)
    assert plan == UpgradePlan([EXECUTABLE, "-m", "pip", "install", "--upgrade", "elitea-mcp"], can_run=True)


def test_venv_without_pip_upgrades_through_uv(env, monkeypatch):
    monkeypatch.setattr(self_update, "is_pip_available", lambda: False)
    plan = resolve_in_venv(env)
    assert plan.command == ["/usr/bin/uv", "pip", "install", "--upgrade", "--python", EXECUTABLE, "elitea-mcp"]
    assert plan.can_run


def test_missing_installer_falls_back_to_a_manual_command(env, monkeypatch):
    (env.prefix / "pipx_metadata.json").write_text("{}")
    monkeypatch.setattr(self_update.shutil, "which", lambda name: None)
    plan = resolve_in_venv(env)
    assert not plan.can_run
    assert plan.display_command == "pipx upgrade elitea-mcp"
    assert plan.reason == "pipx was not found on PATH."


def test_read_only_site_packages_falls_back_to_a_manual_command(env):
    env.site_packages.chmod(0o500)
    try:
        plan = resolve_in_venv(env)
    finally:
        env.site_packages.chmod(0o700)
    assert not plan.can_run
    assert plan.reason == f"No write permission to {env.site_packages}."


@pytest.mark.parametrize("direct_url", [
    {"url": "file:///src/elitea-mcp-client", "dir_info": {"editable": True}},
    {"url": "file:///src/elitea-mcp-client", "dir_info": {}},
    {"url": "https://github.com/EliteaAI/elitea-mcp-client.git", "vcs_info": {"vcs": "git", "commit_id": "49f68cf"}},
])
def test_development_install_is_never_upgraded(env, direct_url):
    env.distribution.direct_url = direct_url
    (env.prefix / "pipx_metadata.json").write_text("{}")
    plan = resolve_in_venv(env)
    assert not plan.can_run and plan.command == []


def test_install_from_a_wheel_file_is_upgraded_normally(env):
    env.distribution.direct_url = {"url": "file:///downloads/elitea_mcp-0.1.35-py3-none-any.whl", "archive_info": {}}
    assert resolve_in_venv(env).can_run


def test_user_site_install_upgrades_with_user_flag(env, monkeypatch):
    monkeypatch.setattr(self_update.site, "ENABLE_USER_SITE", True)
    monkeypatch.setattr(self_update.site, "getusersitepackages", lambda: str(env.site_packages))
    plan = resolve_upgrade_plan(prefix="/base", base_prefix="/base", executable=EXECUTABLE)
    assert plan.command[-1] == "--user" and plan.can_run


def update_paths(tmp_path):
    """Lay out the update files under a temp dir."""
    return {
        "update_helper": tmp_path / "helper.py",
        "update_job": tmp_path / "job.json",
        "update_log": tmp_path / "update.log",
        "update_result": tmp_path / "result.json",
    }


def test_helper_job_relaunches_the_tray_from_the_same_environment(tmp_path, monkeypatch):
    paths = update_paths(tmp_path)
    spawned = MagicMock()
    monkeypatch.setattr(self_update, "spawn_detached", spawned)
    plan = UpgradePlan(["/usr/bin/pipx", "upgrade", "elitea-mcp"], can_run=True)
    self_update.start_update_helper(plan, "0.1.36", "0.1.37", True, False, paths)
    job = json.loads(paths["update_job"].read_text())
    assert job["relaunch"] == [sys.executable, "-m", "elitea_mcp.main", "tray"]
    assert job["upgrade"] == plan.command and job["target"] == "0.1.37" and job["servers_were_running"]
    assert paths["update_helper"].read_text() == HELPER_SCRIPT.read_text()
    helper_command, = spawned.call_args.args
    assert helper_command[1:] == [str(paths["update_helper"]), str(paths["update_job"])]


def run_helper(tmp_path, upgrade_exit_code, current, target):
    """Run the real helper script on an already-exited tray and return its result and relaunch marker."""
    paths = update_paths(tmp_path)
    marker = tmp_path / "relaunched"
    finished_tray = subprocess.Popen([sys.executable, "-c", "pass"])
    finished_tray.wait()
    job = {
        "tray_pid": finished_tray.pid,
        "current": current,
        "target": target,
        "env_python": sys.executable,
        "upgrade": [sys.executable, "-c", f"print('installer output'); raise SystemExit({upgrade_exit_code})"],
        "relaunch": [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ok')"],
        "log": str(paths["update_log"]),
        "result": str(paths["update_result"]),
        "servers_were_running": True,
        "server_daemon_mode": False,
    }
    paths["update_job"].write_text(json.dumps(job))
    subprocess.run([sys.executable, str(HELPER_SCRIPT), str(paths["update_job"])], check=True, timeout=120)
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    return json.loads(paths["update_result"].read_text()), marker, paths


def installed_version():
    """Read the elitea-mcp version the test interpreter sees, which the helper re-reads after upgrading."""
    return subprocess.run([sys.executable, "-c", update_helper.VERSION_PROBE], capture_output=True, text=True).stdout.strip()


def test_helper_upgrades_verifies_and_relaunches(tmp_path):
    result, marker, paths = run_helper(tmp_path, upgrade_exit_code=0, current="0.1.0", target=installed_version())
    assert result["error"] is None and result["installed"] == installed_version()
    assert result["servers_were_running"] is True
    assert marker.exists()
    assert "installer output" in paths["update_log"].read_text()
    assert not paths["update_job"].exists()


def test_helper_reports_a_failed_installer_and_still_relaunches(tmp_path):
    result, marker, _ = run_helper(tmp_path, upgrade_exit_code=1, current="0.1.0", target="9.9.9")
    assert result["error"].endswith("exited with code 1")
    assert result["error"].startswith(f"`{os.path.basename(sys.executable)} -c ")
    assert marker.exists()


def test_helper_accepts_a_release_newer_than_the_target(tmp_path):
    result, _, _ = run_helper(tmp_path, upgrade_exit_code=0, current="0.1.0", target="0.1.1")
    assert result["error"] is None and result["installed"] == installed_version()


def test_helper_treats_a_no_op_upgrade_as_failure(tmp_path):
    result, _, _ = run_helper(tmp_path, upgrade_exit_code=0, current=installed_version(), target="9.9.9")
    assert result["error"].startswith(f"still on {installed_version()} after `{os.path.basename(sys.executable)} -c ")


def test_helper_does_not_relaunch_while_the_tray_is_still_running(tmp_path, monkeypatch):
    paths = update_paths(tmp_path)
    job = {"tray_pid": os.getpid(), "current": "0.1.36", "target": "0.1.37", "env_python": sys.executable,
           "upgrade": ["pipx"], "relaunch": ["tray"], "log": str(paths["update_log"]),
           "result": str(paths["update_result"]), "servers_were_running": False, "server_daemon_mode": False}
    paths["update_job"].write_text(json.dumps(job))
    spawned = MagicMock()
    upgraded = MagicMock()
    monkeypatch.setattr(update_helper, "wait_for_process_exit", lambda pid: False)
    monkeypatch.setattr(update_helper, "spawn_detached", spawned)
    monkeypatch.setattr(update_helper, "run_upgrade", upgraded)
    update_helper.main(str(paths["update_job"]))
    assert json.loads(paths["update_result"].read_text())["error"] == "the tray did not exit in time"
    spawned.assert_not_called()
    upgraded.assert_not_called()


def test_wait_for_process_exit_times_out_on_a_live_process():
    assert update_helper.wait_for_process_exit(os.getpid(), timeout=0.3) is False


def test_update_result_is_reported_once(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({"error": None}))
    assert consume_update_result(result_path) == {"error": None}
    assert consume_update_result(result_path) is None


def build_tray():
    """Create a tray app with an available update and no real servers."""
    bootstrap = {"deployment_url": "https://elitea.example.com", "auth_token": "token",
                 "update_check": True, "update_check_interval_hours": 24}
    controller = MagicMock(daemon_mode=True)
    with patch.object(tray_module, "get_bootstrap_config", return_value=bootstrap), \
            patch.object(tray_module, "get_server_controller", return_value=controller):
        app = tray_module.MCPTrayApp()
    app.update_checker.state = UpdateState("0.1.36", "0.1.37")
    app.icon = MagicMock()
    app.show_notification = MagicMock()
    app.show_error = MagicMock()
    return app


def test_manual_plan_shows_the_command_instead_of_restarting():
    app = build_tray()
    plan = UpgradePlan(["pipx", "upgrade", "elitea-mcp"], reason="pipx was not found on PATH.")
    with patch.object(tray_module, "resolve_upgrade_plan", return_value=plan), \
            patch.object(tray_module, "start_update_helper") as helper, \
            patch.object(app, "_copy_to_clipboard", return_value=True):
        app.update_and_restart()
    helper.assert_not_called()
    app.icon.stop.assert_not_called()
    app.show_notification.assert_called_once_with(
        "Update Manually", "pipx was not found on PATH. Run: pipx upgrade elitea-mcp (copied to clipboard)")


def test_update_stops_servers_then_hands_off_and_quits():
    app = build_tray()
    app.is_server_running = True
    plan = UpgradePlan(["/usr/bin/pipx", "upgrade", "elitea-mcp"], can_run=True)
    calls = []
    app.server_controller.stop_servers.side_effect = lambda: calls.append("stop")
    with patch.object(tray_module, "resolve_upgrade_plan", return_value=plan), \
            patch.object(tray_module, "get_default_paths", return_value={}), \
            patch.object(tray_module, "start_update_helper", side_effect=lambda *a: calls.append(a)):
        app.update_and_restart()
    assert calls == ["stop", (plan, "0.1.36", "0.1.37", True, True, {})]
    app.icon.stop.assert_called_once()


def test_relaunched_tray_reports_the_result_and_restores_servers(tmp_path):
    app = build_tray()
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({"error": None, "installed": "0.1.37", "log": "update.log",
                                       "servers_were_running": True, "server_daemon_mode": True}))
    with patch.object(tray_module, "get_default_paths", return_value={"update_result": result_path}), \
            patch.object(tray_module, "get_bootstrap_config",
                         return_value={"deployment_url": "https://elitea.example.com", "auth_token": "token"}):
        app.report_update_result()
    app.show_notification.assert_called_once_with("Update Complete", "Updated to 0.1.37.")
    app.server_controller.start_servers.assert_called_once_with(daemon_mode=True)


def test_relaunched_tray_reports_a_failure_with_the_log_path(tmp_path):
    app = build_tray()
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({"error": "pipx exited with code 1", "installed": "0.1.36",
                                       "log": "/logs/update.log", "servers_were_running": False,
                                       "server_daemon_mode": False}))
    with patch.object(tray_module, "get_default_paths", return_value={"update_result": result_path}):
        app.report_update_result()
    app.show_error.assert_called_once_with("Update Failed", "pipx exited with code 1. See log: /logs/update.log")
    app.server_controller.start_servers.assert_not_called()
