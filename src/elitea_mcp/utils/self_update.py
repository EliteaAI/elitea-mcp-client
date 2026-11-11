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


def plan_with_tool(tool, arguments):
    """Plan an upgrade through an installer binary, falling back to manual when it cannot be found."""
    tool_path = shutil.which(tool)
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


SOURCE_CHECKOUT = "source checkout"
DEVELOPMENT = "development install"
PIPX = "pipx"
UV_TOOL = "uv tool"
VIRTUAL_ENV = "virtual environment"
USER_SITE = "pip --user"
SYSTEM_SITE = "pip (system Python)"


def detect_install_method(prefix=sys.prefix, base_prefix=sys.base_prefix):
    """Name how elitea-mcp was installed, with its distribution, or None from an uninstalled checkout."""
    try:
        distribution = importlib.metadata.distribution(package_name)
    except importlib.metadata.PackageNotFoundError:
        return SOURCE_CHECKOUT, None
    if is_development_install(distribution):
        return DEVELOPMENT, distribution
    if Path(prefix, "pipx_metadata.json").exists():
        return PIPX, distribution
    if Path(prefix, "uv-receipt.toml").exists():
        return UV_TOOL, distribution
    if prefix != base_prefix:
        return VIRTUAL_ENV, distribution
    if is_user_site_install(distribution):
        return USER_SITE, distribution
    return SYSTEM_SITE, distribution


def resolve_upgrade_plan(prefix=sys.prefix, base_prefix=sys.base_prefix, executable=sys.executable):
    """Pick the upgrade command matching how elitea-mcp was installed, or explain why it must be run by hand."""
    method, distribution = detect_install_method(prefix, base_prefix)
    if method == SOURCE_CHECKOUT:
        return UpgradePlan(reason="elitea-mcp is running from a source checkout; update it with git.")
    if method == DEVELOPMENT:
        return UpgradePlan(reason="This is a development install; update it with git.")
    if method == PIPX:
        return plan_with_tool("pipx", ["upgrade", package_name])
    if method == UV_TOOL:
        return plan_with_tool("uv", ["tool", "install", f"{package_name}@latest"])
    pip_command = [executable, "-m", "pip", "install", "--upgrade", package_name]
    if method == VIRTUAL_ENV:
        if is_pip_available():
            return plan_with_pip(pip_command, distribution)
        return plan_with_tool("uv", ["pip", "install", "--upgrade", "--python", executable, package_name])
    if method == USER_SITE:
        return plan_with_pip([*pip_command, "--user"], distribution)
    return plan_with_pip(pip_command, distribution)


def build_relaunch_command():
    """Start the tray again from the same environment; the helper already runs it detached."""
    return [sys.executable, "-m", "elitea_mcp.main", "tray"]


def start_update_helper(plan, current, target, servers_were_running, server_daemon_mode, paths):
    """Copy the helper out of the package and start it detached, so the upgrade can replace the package."""
    os.umask(os.umask(0) | 0o022)
    helper_path = Path(paths["update_helper"])
    helper_path.write_text(Path(update_helper.__file__).read_text())
    job = {
        "tray_pid": os.getpid(),
        "current": current,
        "target": target,
        "env_python": sys.executable,
        "upgrade": plan.command,
        "relaunch": build_relaunch_command(),
        "log": str(paths["update_log"]),
        "result": str(paths["update_result"]),
        "servers_were_running": servers_were_running,
        "server_daemon_mode": server_daemon_mode,
    }
    Path(paths["update_job"]).write_text(json.dumps(job))
    spawn_detached([getattr(sys, "_base_executable", sys.executable), str(helper_path), str(paths["update_job"])])


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
