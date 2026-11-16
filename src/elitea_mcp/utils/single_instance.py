"""One ELITEA MCP process per user: the tray or `serve` holds client_id.lock for its whole life, and a second copy is refused."""
import atexit
import ctypes
import errno
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from ..config import write_file_atomically
from . import client_identity
from .logging_setup import OWNER_ONLY_FILE_MODE, SERVE, TRAY, restrict_to_owner

OWNER_FILE_NAME = "client_id.owner.json"
LOCK_ATTEMPTS = 3
LOCK_RETRY_SECONDS = 0.05
UNAVAILABLE = object()
WINDOWS_SYNCHRONIZE = 0x00100000
WINDOWS_WAIT_TIMEOUT = 0x00000102

logger = logging.getLogger(__name__)


class AlreadyRunningError(Exception):
    """Another process holds the instance lock; the text names its PID and command."""


class LockUnavailable(Exception):
    """The lock cannot be used at all, for example on a network home without lock support, so nobody can be detected."""


@dataclass
class Holder:
    pid: Optional[int]
    command: str


def get_lock_path():
    return client_identity.get_config_dir() / client_identity.CLIENT_ID_LOCK_NAME


def get_owner_path():
    return client_identity.get_config_dir() / OWNER_FILE_NAME


def is_contention(error):
    """Only another holder counts as running; Windows reports a held byte as EACCES."""
    if client_identity.fcntl is not None:
        return isinstance(error, BlockingIOError)
    return error.errno == errno.EACCES


def open_lock_file(path):
    """Open the lock file owner-only; any error opening it means the lock cannot be used, never that a copy runs."""
    try:
        handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, OWNER_ONLY_FILE_MODE), "a+")
    except OSError as error:
        raise LockUnavailable(f"Could not open {path}: {error}") from error
    restrict_to_owner(path, OWNER_ONLY_FILE_MODE)
    return handle


def take_lock(handle, path):
    """Lock in a few tries, because a peek holds the lock for an instant; False when another process holds it."""
    for attempt in range(LOCK_ATTEMPTS):
        try:
            client_identity.lock_exclusively(handle)
            return True
        except OSError as error:
            if not is_contention(error):
                raise LockUnavailable(f"Could not lock {path}: {error}") from error
        if attempt + 1 < LOCK_ATTEMPTS:
            time.sleep(LOCK_RETRY_SECONDS)
    return False


def hold_instance(command):
    """Hold the lock for the rest of this process, or raise AlreadyRunningError; a lock that cannot be used only warns."""
    path = get_lock_path()
    if client_identity.is_held_here(path):
        return
    try:
        handle = open_lock_file(path)
    except LockUnavailable as error:
        warn_undetectable(error)
        return
    try:
        taken = take_lock(handle, path)
    except LockUnavailable as error:
        handle.close()
        warn_undetectable(error)
        return
    if not taken:
        handle.close()
        raise AlreadyRunningError(describe_holder(read_holder()))
    client_identity.keep_until_exit(path, handle)
    write_owner(command)


def run_as_extra_copy():
    """Skip the single-instance lock for --allow-multiple; the platform still declines servers whose names another copy registered."""
    client_identity.skip_lock()
    logger.warning("Running as an extra copy (--allow-multiple): servers with the same names as in another running copy "
                   "are declined by the platform.")


def warn_undetectable(error):
    logger.warning(f"{error}. Another running copy of the client cannot be detected.")


def write_owner(command):
    """Remove the last holder's file first, so a failed write leaves "PID unknown" and never a dead PID."""
    path = get_owner_path()
    try:
        path.unlink(missing_ok=True)
        write_file_atomically(path, json.dumps({"pid": os.getpid(), "command": command, "started_at": time.time()}))
    except OSError as error:
        logger.warning(f"Could not write {path}: {error}")
        return
    atexit.register(remove_owner_if_mine, path, os.getpid())


def remove_owner_if_mine(path, pid):
    """Remove the owner file at exit while it still names this process, so its PID is never shown once the system reuses it."""
    try:
        owner = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(owner, dict) and owner.get("pid") == pid:
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def read_holder():
    """Who holds the lock, as its owner file says; a client older than this check writes none, so its PID is unknown."""
    try:
        owner = json.loads(get_owner_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Holder(None, "")
    pid, command = (owner.get("pid"), owner.get("command")) if isinstance(owner, dict) else (None, None)
    if type(pid) is not int or pid <= 0 or command not in (TRAY, SERVE) or not is_alive(pid):
        return Holder(None, "")
    return Holder(pid, command)


def is_alive(pid):
    """Whether the PID an owner file names still runs, as the file outlives a holder and an older client writes none."""
    if os.name == "nt":
        return is_windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_windows_process_alive(pid):
    """Probe the process handle, because os.kill on Windows terminates the process instead of probing it."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(WINDOWS_SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WINDOWS_WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def find_holder():
    """Peek without keeping the lock: a Holder, None when it is free, or UNAVAILABLE when it cannot be used."""
    path = get_lock_path()
    if client_identity.is_held_here(path):
        return read_holder()
    if not path.exists():
        return None
    try:
        handle = open_lock_file(path)
    except LockUnavailable:
        return UNAVAILABLE
    with handle:
        try:
            taken = take_lock(handle, path)
        except LockUnavailable:
            return UNAVAILABLE
        if not taken:
            return read_holder()
        client_identity.unlock(handle)
    return None


def release_instance():
    """Let go of the lock; for tests only, as a process otherwise keeps it until the system ends the process."""
    client_identity.release_held_lock(get_lock_path())


def describe_holder(holder):
    """The refusal: fixed text, with the PID for display only."""
    if holder.pid is None:
        return 'ELITEA MCP Client is already running (PID unknown). Run "elitea-mcp doctor" to find it.'
    if holder.command == TRAY:
        return (f"ELITEA MCP Client is already running (PID {holder.pid}, tray). Quit it from its tray icon, "
                f"or end process {holder.pid} if it does not respond.")
    return (f"ELITEA MCP Client is already running (PID {holder.pid}, serve). Stop it first "
            f"(Ctrl+C in its terminal, or end process {holder.pid}).")
