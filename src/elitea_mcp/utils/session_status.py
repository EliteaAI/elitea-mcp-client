"""The running client's server status in status.json, so `elitea-mcp doctor` in another process can show the live session."""
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from ..config import write_file_atomically
from .client_identity import (SESSION_STATUS_FILE_NAME, get_client_lock_path, holds_client_lock,
                              is_client_lock_held_elsewhere)
from .server_loader import ServerFailure
from .server_registry import ServerEntry

STATUS_FILE_NAME = SESSION_STATUS_FILE_NAME
HEARTBEAT_SECONDS = 15
STALE_AFTER_SECONDS = 60

logger = logging.getLogger(__name__)


def get_status_file():
    """Next to the client lock, whose holder is the only writer."""
    return get_client_lock_path().parent / STATUS_FILE_NAME


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
    """Rewrite status.json on every server change and on a heartbeat, but only from the process that holds the client lock."""

    def __init__(self, list_entries, is_connected):
        self.list_entries = list_entries
        self.is_connected = is_connected
        self._write_lock = threading.Lock()
        self._stopped = threading.Event()
        self._heartbeat = None

    def publish(self):
        self.write({"running": True, "connected": bool(self.is_connected()),
                    "servers": [describe_entry(entry) for entry in self.list_entries()]})

    def write(self, status, final=False):
        """Write under one lock, so a change racing with stop() can never overwrite the stopped state."""
        with self._write_lock:
            if self._stopped.is_set() and not final:
                return
            if final:
                self._stopped.set()
            if not holds_client_lock():
                return
            status = {"pid": os.getpid(), "updated_at": time.time(), **status}
            try:
                write_file_atomically(get_status_file(), json.dumps(status, indent=2))
            except OSError as error:
                logger.debug(f"Could not write {get_status_file()}: {error}")

    def start(self):
        self.publish()
        self._heartbeat = threading.Thread(target=self._beat, name="session-status-heartbeat", daemon=True)
        self._heartbeat.start()

    def _beat(self):
        while not self._stopped.wait(HEARTBEAT_SECONDS):
            self.publish()

    def stop(self):
        """The servers stopped, but the process keeps the client lock until it exits, so say so instead of going quiet."""
        self.write({"running": False, "connected": False, "servers": []}, final=True)


@dataclass
class RunningSession:
    """What another running client published, or why it cannot be shown."""
    entries: Optional[list] = None
    running: bool = True
    connected: bool = False
    age: Optional[float] = None
    problem: str = ""


def read_running_session():
    """The session of another running client; None when no other client holds the lock."""
    if not is_client_lock_held_elsewhere():
        return None
    try:
        status = json.loads(get_status_file().read_text(encoding="utf-8"))
        updated_at = float(status["updated_at"])
        running = bool(status.get("running", True))
        entries = [restore_entry(item) for item in status["servers"]]
    except FileNotFoundError:
        return RunningSession(problem="it has not published its server status (it may still be starting, or be an older version)")
    except (OSError, ValueError, KeyError, TypeError):
        return RunningSession(problem="its status file could not be read")
    age = max(0.0, time.time() - updated_at)
    if not running:
        return RunningSession(entries=[], running=False, age=age)
    if age > STALE_AFTER_SECONDS:
        return RunningSession(age=age, problem=f"its status has not been updated for {round(age)} s (it may be frozen)")
    return RunningSession(entries=entries, connected=bool(status.get("connected")), age=age)
