"""
Upgrade elitea-mcp once the tray has exited, then relaunch the tray.

Runs as a standalone script on the base interpreter, so it imports only the standard library.
"""
import ctypes
import json
import os
import subprocess
import sys
import time
from datetime import datetime

TRAY_EXIT_TIMEOUT_SECONDS = 60
UPGRADE_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 0.2
WINDOWS_SYNCHRONIZE = 0x00100000
WINDOWS_WAIT_OBJECT_0 = 0
WINDOWS_DETACHED_PROCESS = 0x00000008
WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
VERSION_PROBE = "import importlib.metadata as m; print(m.version('elitea-mcp'))"


def spawn_detached(command, env=None):
    """Start a process that keeps running after its parent exits."""
    if os.name == "nt":
        platform_options = {"creationflags": WINDOWS_DETACHED_PROCESS | WINDOWS_CREATE_NEW_PROCESS_GROUP}
    else:
        platform_options = {"start_new_session": True}
    return subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **platform_options,
    )


def wait_for_windows_process_exit(pid, timeout):
    """Wait on the process handle, because os.kill on Windows terminates the process instead of probing it."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(WINDOWS_SYNCHRONIZE, False, pid)
    if not handle:
        return True
    try:
        return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == WINDOWS_WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


def wait_for_process_exit(pid, timeout=TRAY_EXIT_TIMEOUT_SECONDS):
    """Block until the tray is gone so the installer can replace its files."""
    if os.name == "nt":
        return wait_for_windows_process_exit(pid, timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(POLL_INTERVAL_SECONDS)
    return False


def describe_command(command):
    """Name the installer command the way a user would type it, without the full interpreter path."""
    return " ".join([os.path.basename(command[0]), *command[1:]])


def run_upgrade(command, log):
    """Run the installer with its output in the log, returning the failure reason or None."""
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   timeout=UPGRADE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return f"{command[0]} did not finish within {UPGRADE_TIMEOUT_SECONDS} seconds"
    except OSError as error:
        return f"could not run {command[0]}: {error}"
    if completed.returncode != 0:
        return f"`{describe_command(command)}` exited with code {completed.returncode}"
    return None


def read_installed_version(env_python):
    """Read the version a fresh interpreter of the tray's environment now sees."""
    try:
        completed = subprocess.run([env_python, "-c", VERSION_PROBE], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() or None


def upgrade_and_verify(job, log):
    """Upgrade, then judge success by the version moving, because installers can exit 0 without upgrading and may install a release newer than the target."""
    error = run_upgrade(job["upgrade"], log)
    installed = read_installed_version(job["env_python"])
    if error is None and installed == job["current"]:
        error = f"still on {installed} after `{describe_command(job['upgrade'])}`"
    return installed, error


def main(job_path):
    """Run one update job and leave the outcome where the relaunched tray reports it."""
    with open(job_path) as job_file:
        job = json.load(job_file)
    with open(job["log"], "a") as log:
        log.write(f"--- {datetime.now().isoformat(timespec='seconds')} updating elitea-mcp "
                  f"{job['current']} -> {job['target']}: {' '.join(job['upgrade'])}\n")
        log.flush()
        tray_exited = wait_for_process_exit(job["tray_pid"])
        installed, error = upgrade_and_verify(job, log) if tray_exited else (None, "the tray did not exit in time")
        log.write(f"--- result: installed={installed} error={error}\n")
    result = {
        "previous": job["current"],
        "target": job["target"],
        "installed": installed,
        "error": error,
        "log": job["log"],
        "servers_were_running": job["servers_were_running"],
        "server_daemon_mode": job["server_daemon_mode"],
    }
    with open(job["result"], "w") as result_file:
        json.dump(result, result_file)
    os.remove(job_path)
    if tray_exited:
        spawn_detached(job["relaunch"])


if __name__ == "__main__":
    main(sys.argv[1])
