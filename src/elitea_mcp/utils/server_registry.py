"""The state of every configured server in this run: the one source for the tray, the console and platform snapshots."""
import threading
from dataclasses import dataclass, field
from typing import Optional

from .server_loader import AUTH, COMMAND_NOT_FOUND, INVALID_CONFIG, TIMEOUT, UNREACHABLE, ServerFailure

PENDING = "pending"
HEALTHY = "healthy"
FAILED = "failed"
REGISTERED = "registered"
DECLINED = "declined"
FAILURES_BEFORE_MARKED_FAILED = 2
NEVER_RETRIED = frozenset({COMMAND_NOT_FOUND, INVALID_CONFIG, AUTH})
TRANSIENT_AT_START = frozenset({TIMEOUT, UNREACHABLE})
DECLINE_REASONS = {"name_already_registered": "name already registered", "invalid_name": "invalid name"}


@dataclass
class ServerEntry:
    name: str
    transport: str
    status: str = PENDING
    tools: list = field(default_factory=list)
    failure: Optional[ServerFailure] = None
    stderr_tail: list = field(default_factory=list)
    registration: Optional[str] = None
    decline_reason: str = ""
    transport_failures: int = 0
    was_healthy: bool = False
    retry_attempts: int = 0
    retry_stopped: bool = False

    @property
    def message(self):
        return self.failure.describe(self.name, self.transport) if self.failure else ""

    @property
    def can_auto_retry(self):
        """Retry only what may fix itself: never a missing command, a broken config or a rejected token."""
        if self.status != FAILED or self.failure.kind in NEVER_RETRIED:
            return False
        return self.was_healthy or self.failure.kind in TRANSIENT_AT_START

    def describe_status(self):
        if self.status == FAILED:
            suffix = " (retry stopped)" if self.retry_stopped else ""
            return f"failed: {self.failure.reason}{suffix}"
        if self.status == PENDING:
            return "starting"
        if self.registration == DECLINED:
            return f"declined ({DECLINE_REASONS.get(self.decline_reason, self.decline_reason)})"
        tools = f"{len(self.tools)} tool{'' if len(self.tools) == 1 else 's'}"
        return f"{tools}, {self.registration or 'not registered yet'}"

    @property
    def mark(self):
        return {FAILED: "✗", PENDING: "…"}.get(self.status, "⚠" if self.registration == DECLINED else "✓")

    def format_menu_row(self):
        return f"{self.mark} {self.name} — {self.describe_status()}"


class ServerRegistry:
    def __init__(self, on_change=None):
        self._lock = threading.RLock()
        self._entries = {}
        self._on_change = on_change or (lambda: None)

    def add_pending(self, name, transport):
        with self._lock:
            self._entries[name] = ServerEntry(name, transport)

    def add_invalid(self, name, transport, problem):
        with self._lock:
            self._entries[name] = ServerEntry(name, transport, status=FAILED,
                                              failure=ServerFailure(INVALID_CONFIG, f"invalid configuration: {problem}",
                                                                    "Fix it in config.json and restart."))
        self._on_change()

    def record_load(self, load):
        """Store a load result; a recovered server keeps its registration, a failed one loses its tools."""
        with self._lock:
            entry = self._entries.setdefault(load.name, ServerEntry(load.name, load.transport))
            entry.transport_failures = 0
            if load.ok:
                entry.status, entry.tools, entry.failure, entry.stderr_tail = HEALTHY, load.tools, None, []
                entry.was_healthy, entry.retry_attempts, entry.retry_stopped = True, 0, False
            else:
                entry.status, entry.tools, entry.failure, entry.stderr_tail = FAILED, [], load.failure, load.stderr_tail
        self._on_change()

    def record_transport_failure(self, name, failure, stderr_tail=()):
        """Count a failure to start or reach a registered server; return True when this one marks it failed."""
        with self._lock:
            entry = self._entries.get(name)
            if entry is None or entry.status != HEALTHY:
                return False
            entry.transport_failures += 1
            if entry.transport_failures < FAILURES_BEFORE_MARKED_FAILED:
                return False
            entry.status, entry.failure, entry.stderr_tail = FAILED, failure, list(stderr_tail)
            entry.retry_attempts, entry.retry_stopped = 0, False
        self._on_change()
        return True

    def record_tools(self, name, tools):
        """Replace a healthy server's tools; return True when they changed."""
        with self._lock:
            entry = self._entries.get(name)
            if entry is None or entry.status != HEALTHY or entry.tools == tools:
                return False
            entry.tools = tools
        self._on_change()
        return True

    def record_call_success(self, name):
        with self._lock:
            entry = self._entries.get(name)
            if entry is not None:
                entry.transport_failures = 0

    def record_retry_scheduled(self, name):
        with self._lock:
            self._entries[name].retry_attempts += 1

    def reset_retries(self, name):
        with self._lock:
            self._entries[name].retry_attempts, self._entries[name].retry_stopped = 0, False

    def record_retry_stopped(self, name):
        with self._lock:
            self._entries[name].retry_stopped = True
        self._on_change()

    def apply_registration(self, sent_names, registered, declined, keep_own_names=False):
        """Record the platform's answer; on an old platform a decline of a name this client already holds still counts as registered."""
        reasons = {item.get("name"): item.get("reason", "") for item in declined if isinstance(item, dict)}
        with self._lock:
            for name in sent_names:
                entry = self._entries.get(name)
                if entry is None:
                    continue
                if name in registered or (keep_own_names and name in reasons and entry.registration == REGISTERED):
                    entry.registration, entry.decline_reason = REGISTERED, ""
                elif name in reasons:
                    entry.registration, entry.decline_reason = DECLINED, reasons[name]
        self._on_change()

    def mark_all_registered(self, sent_names):
        self.apply_registration(sent_names, set(sent_names), [])

    def forget_registrations(self):
        """A new socket owns nothing on the platform yet."""
        with self._lock:
            for entry in self._entries.values():
                entry.registration, entry.decline_reason = None, ""

    def get(self, name):
        with self._lock:
            return self._entries.get(name)

    def list_entries(self):
        with self._lock:
            return list(self._entries.values())

    def list_by_status(self, status):
        return [entry for entry in self.list_entries() if entry.status == status]

    def summarize(self, suffix=""):
        """One line such as "3 of 4 MCP servers connected. Failed: confluence (command "uvx" not found)."."""
        entries = self.list_entries()
        connected = [entry for entry in entries if entry.status == HEALTHY and entry.registration != DECLINED]
        failed = [f"{entry.name} ({entry.failure.reason})" for entry in entries if entry.status == FAILED]
        declined = [f"{entry.name} ({entry.describe_status()})" for entry in entries
                    if entry.status == HEALTHY and entry.registration == DECLINED]
        parts = [f"{len(connected)} of {len(entries)} MCP servers connected."]
        if failed:
            parts.append(f"Failed: {', '.join(failed)}.")
        if declined:
            parts.append(f"Declined: {', '.join(declined)}.")
        if suffix and (failed or declined):
            parts.append(suffix)
        return " ".join(parts)
