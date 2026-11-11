"""The running session's server status in status.json, so `elitea-mcp doctor` in another process can show it."""
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from ..config import write_file_atomically
from . import client_identity
from .server_loader import ServerFailure
from .server_registry import ServerEntry

SESSION_LOCK_NAME = "session.lock"
STATUS_FILE_NAME = "status.json"
HEARTBEAT_SECONDS = 15
STALE_AFTER_SECONDS = 60

_sessions_held_here = set()

logger = logging.getLogger(__name__)


def get_session_lock_path():
    """Next to client_id.lock, which says who owns the client id for the life of a process; this one says whose servers run now."""
    return client_identity.get_config_dir() / SESSION_LOCK_NAME


def get_status_file():
    return client_identity.get_config_dir() / STATUS_FILE_NAME


def describe_entry(entry):
    return {
        "name": entry.name,
        "transport": entry.transport,
        "status": entry.status,
        "tools": [tool.get("name", "") for tool in entry.tools],
        "registration": entry.registration,
        "decline_reason": entry.decline_reason,
        "retry_scheduled": entry.retry_scheduled,
        "failure": None if entry.failure is None else {
            "kind": entry.failure.kind, "reason": entry.failure.reason, "hint": entry.failure.hint},
    }


def restore_entry(item):
    failure = item.get("failure")
    return ServerEntry(
        name=item["name"], transport=item.get("transport", "stdio"), status=item["status"],
        tools=[{"name": name} for name in item.get("tools", [])],
        failure=ServerFailure(failure["kind"], failure["reason"], failure.get("hint", "")) if failure else None,
        registration=item.get("registration"), decline_reason=item.get("decline_reason", ""),
        retry_scheduled=bool(item.get("retry_scheduled")),
    )


class SessionStatusPublisher:
    """Hold the session lock while servers run and keep status.json current; a run that cannot take the lock publishes nothing."""

    def __init__(self, list_entries, is_connected):
        self.list_entries = list_entries
        self.is_connected = is_connected
        self._write_lock = threading.Lock()
        self._stopped = threading.Event()
        self._session_lock = None
        self._lock_path = None
        self._status_file = None

    def start(self):
        """Publish from the moment servers start loading, and on a heartbeat, so a slow start never looks frozen."""
        self.publish()
        threading.Thread(target=self._beat, name="session-status-heartbeat", daemon=True).start()

    def _beat(self):
        while not self._stopped.wait(HEARTBEAT_SECONDS):
            self.publish()

    def publish(self):
        """Read the servers under the write lock, so a snapshot taken earlier can never land after a newer one."""
        with self._write_lock:
            if self._stopped.is_set() or not self._hold_session():
                return
            status = {"pid": os.getpid(), "connected": bool(self.is_connected()),
                      "servers": [describe_entry(entry) for entry in self.list_entries()]}
            try:
                write_file_atomically(self._status_file, json.dumps({**status, "updated_at": time.time()}, indent=2))
            except OSError as error:
                logger.debug(f"Could not write {self._status_file}: {error}")

    def _hold_session(self):
        """Take the session lock when free, for example once a run that held it stopped; the first write follows at once, under the same lock."""
        if self._session_lock is None:
            lock_path = get_session_lock_path()
            self._session_lock = client_identity.take_lock(lock_path)
            if self._session_lock is not None:
                self._lock_path, self._status_file = lock_path, get_status_file()
                _sessions_held_here.add(lock_path)
        return self._session_lock is not None

    def stop(self):
        with self._write_lock:
            self._stopped.set()
            if self._session_lock is None:
                return
            try:
                self._status_file.unlink(missing_ok=True)
            except OSError as error:
                logger.debug(f"Could not remove {self._status_file}: {error}")
            _sessions_held_here.discard(self._lock_path)
            client_identity.release_lock(self._session_lock)
            self._session_lock = None


@dataclass
class RunningSession:
    """What a running session published, or why it cannot be shown."""
    entries: Optional[list] = None
    connected: bool = False
    age: Optional[float] = None
    problem: str = ""


def read_running_session():
    """Another process's session running with this config right now; None when there is none or this process runs it."""
    lock_path = get_session_lock_path()
    if lock_path in _sessions_held_here or not client_identity.is_lock_held_elsewhere(lock_path):
        return None
    try:
        status = json.loads(get_status_file().read_text(encoding="utf-8"))
        updated_at = float(status["updated_at"])
        entries = [restore_entry(item) for item in status["servers"]]
    except FileNotFoundError:
        return RunningSession(problem="it has not published its server status yet")
    except (OSError, ValueError, KeyError, TypeError):
        return RunningSession(problem="its status file could not be read")
    age = max(0.0, time.time() - updated_at)
    if age > STALE_AFTER_SECONDS:
        return RunningSession(age=age, problem=f"its status has not been updated for {round(age)} s (it may be frozen)")
    return RunningSession(entries=entries, connected=bool(status.get("connected")), age=age)
