"""
Choose the upgrade command for this install and hand the upgrade to a helper that outlives the tray.
"""
import importlib.metadata
import importlib.util
import json
import os
import shlex
import shutil
import site
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from .package_updates import package_name
from . import update_helper
from .update_helper import spawn_detached

TOOL_DIRS_MISSING_FROM_GUI_PATH = ("~/.local/bin", "~/.cargo/bin", "/opt/homebrew/bin", "/usr/local/bin")
DAEMON_SUBPROCESS_ENV = "ELITEA_MCP_DAEMON_MODE"


@dataclass(frozen=True)
class UpgradePlan:
    command: List[str] = field(default_factory=list)
    can_run: bool = False
    reason: str = ""

    @property
    def display_command(self) -> str:
        """Render the command the way the user's shell expects it."""
        if os.name == "nt":
            return subprocess.list2cmdline(self.command)
        return shlex.join(self.command)


def find_tool(name):
    """Locate an installer binary, including the usual user dirs that a GUI-launched tray's PATH lacks."""
    extra_dirs = [os.path.expanduser(directory) for directory in TOOL_DIRS_MISSING_FROM_GUI_PATH]
    return shutil.which(name, path=os.pathsep.join([os.environ.get("PATH", ""), *extra_dirs]))


def plan_with_tool(tool, arguments):
    """Plan an upgrade through an installer binary, falling back to manual when it cannot be found."""
    tool_path = find_tool(tool)
    if tool_path:
        return UpgradePlan([tool_path, *arguments], can_run=True)
    return UpgradePlan([tool, *arguments], reason=f"{tool} was not found on PATH.")


def plan_with_pip(command, distribution):
    """Plan a pip upgrade, falling back to manual when pip is missing or site-packages is read-only."""
    site_packages = str(distribution.locate_file(""))
    if not is_pip_available():
        return UpgradePlan(command, reason="pip is not installed in this environment.")
    if not os.access(site_packages, os.W_OK):
        return UpgradePlan(command, reason=f"No write permission to {site_packages}.")
    return UpgradePlan(command, can_run=True)


def is_pip_available():
    """Tell whether the running environment can run `python -m pip`."""
    return importlib.util.find_spec("pip") is not None


def is_development_install(distribution):
    """Tell whether elitea-mcp was installed from a source directory or git, which only git should update."""
    direct_url = distribution.read_text("direct_url.json")
    if not direct_url:
        return False
    origin = json.loads(direct_url)
    return "dir_info" in origin or "vcs_info" in origin


def is_user_site_install(distribution):
    """Tell whether elitea-mcp lives in the user site-packages of a system interpreter."""
    return bool(site.ENABLE_USER_SITE) and (
        Path(distribution.locate_file("")).resolve() == Path(site.getusersitepackages()).resolve()
    )


def resolve_upgrade_plan(prefix=sys.prefix, base_prefix=sys.base_prefix, executable=sys.executable):
    """Pick the upgrade command matching how elitea-mcp was installed, or explain why it must be run by hand."""
    try:
        distribution = importlib.metadata.distribution(package_name)
    except importlib.metadata.PackageNotFoundError:
        return UpgradePlan(reason="elitea-mcp is running from a source checkout; update it with git.")
    if is_development_install(distribution):
        return UpgradePlan(reason="This is a development install; update it with git.")
    if Path(prefix, "pipx_metadata.json").exists():
        return plan_with_tool("pipx", ["upgrade", package_name])
    if Path(prefix, "uv-receipt.toml").exists():
        return plan_with_tool("uv", ["tool", "install", f"{package_name}@latest"])
    pip_command = [executable, "-m", "pip", "install", "--upgrade", package_name]
    if prefix != base_prefix:
        if is_pip_available():
            return plan_with_pip(pip_command, distribution)
        return plan_with_tool("uv", ["pip", "install", "--upgrade", "--python", executable, package_name])
    if is_user_site_install(distribution):
        return plan_with_pip([*pip_command, "--user"], distribution)
    return plan_with_pip(pip_command, distribution)


def build_relaunch_command(tray_flags):
    """Start the tray again from the same environment with the flags it was launched with."""
    return [sys.executable, "-m", "elitea_mcp.main", "tray", *tray_flags]


def start_update_helper(plan, current, target, tray_flags, servers_were_running, server_daemon_mode, paths):
    """Copy the helper out of the package and start it detached, so the upgrade can replace the package."""
    helper_path = Path(paths["update_helper"])
    helper_path.write_text(Path(update_helper.__file__).read_text())
    job = {
        "tray_pid": os.getpid(),
        "current": current,
        "target": target,
        "env_python": sys.executable,
        "upgrade": plan.command,
        "relaunch": build_relaunch_command(tray_flags),
        "log": str(paths["update_log"]),
        "result": str(paths["update_result"]),
        "servers_were_running": servers_were_running,
        "server_daemon_mode": server_daemon_mode,
    }
    Path(paths["update_job"]).write_text(json.dumps(job))
    relaunch_env = {key: value for key, value in os.environ.items() if key != DAEMON_SUBPROCESS_ENV}
    spawn_detached([getattr(sys, "_base_executable", sys.executable), str(helper_path), str(paths["update_job"])],
                   env=relaunch_env)


def consume_update_result(result_path):
    """Read and remove the outcome of the last update, so it is reported once."""
    path = Path(result_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None
    finally:
        path.unlink(missing_ok=True)
