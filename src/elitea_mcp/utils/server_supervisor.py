"""Run the configured servers for one session: load each on its own, register the healthy ones, retry the failed ones."""
import asyncio
import logging
import random
import threading

from ..config_model import DEFAULT_STARTUP_TIMEOUT, SKIPPED
from .name import sanitize
from .server_loader import collect_secret_values, load_servers, mask_secrets, read_transport
from .server_registry import FAILED, HEALTHY, ServerRegistry
from .session_status import SessionStatusPublisher
from .sio import start_socket_connection

RETRY_DELAYS = (30, 60, 120, 300)
CONSOLE_STDERR_LINES = 5

logger = logging.getLogger(__name__)


def collect_skipped_servers(report, raw_config):
    """Name each server the config check skipped, with the transport it asked for and the first reason it cannot run."""
    raw_servers = raw_config.get("servers") if isinstance(raw_config.get("servers"), dict) else {}
    skipped = []
    for raw_name in report.skipped_servers:
        path = f"servers.{raw_name}"
        problem = next((problem for problem in report.errors if problem.path == path or problem.path.startswith(f"{path}.")), None)
        detail = problem.message.removesuffix(SKIPPED).strip().rstrip(".") if problem else "cannot run"
        field = problem.path[len(path) + 1:] if problem and problem.path != path else ""
        raw = raw_servers.get(raw_name)
        transport = str(raw.get("type") or "stdio") if isinstance(raw, dict) else "stdio"
        skipped.append((sanitize(raw_name) or raw_name, transport, f"{field}: {detail}" if field else detail))
    return skipped


def calculate_retry_delay(entry):
    """The next step of the schedule with equal jitter, so clients hit by one outage do not retry together, never sooner than Retry-After."""
    step = RETRY_DELAYS[min(entry.retry_attempts, len(RETRY_DELAYS) - 1)]
    delay = step / 2 + random.uniform(0, step / 2)
    return max(delay, entry.failure.retry_after or 0)


def format_error_report(entry, output):
    """The text of <server>-error.txt: status, reason, hint and the server's last output with secrets already hidden."""
    lines = [f'Server "{entry.name}" ({entry.transport})', f"Status: {entry.describe_status()}"]
    if entry.failure:
        lines += [f"Reason: {entry.failure.reason}", f"Hint: {entry.failure.hint}"]
    if entry.status != FAILED and entry.decline_reason:
        lines.append("Hint: another running client, or a session that has not timed out yet, holds this name in the "
                     "project. Stop it or rename the server in config.json.")
    if output:
        lines += ["", "Last output:", *(f"    {line}" for line in output)]
    return "\n".join(lines) + "\n"


def ignore_message(message):
    """Stand in for a notification callback the caller did not provide."""


class ServerSupervisor:
    def __init__(self, config, skipped=(), notify_status=None, notify_error=None, notify_server=None, on_change=None,
                 summary_suffix="", cancelled=None):
        self.config = config
        self.cancelled = cancelled or threading.Event()
        self.notify_status = notify_status or ignore_message
        self.notify_error = notify_error or ignore_message
        self.notify_server = notify_server or ignore_message
        self.summary_suffix = summary_suffix
        self.on_change = on_change or (lambda: None)
        self.link = None
        self.session_status = SessionStatusPublisher(lambda: self.registry.list_entries(),
                                                     lambda: self.link is not None and self.link.connected)
        self.registry = ServerRegistry(self.handle_registry_change)
        self._timers = {}
        self._retry_lock = threading.Lock()
        self._stopped = False
        for name, server_conf in config["servers"].items():
            self.registry.add_pending(name, read_transport(server_conf))
        for name, transport, problem in skipped:
            self.registry.add_invalid(name, transport, problem)

    def handle_registry_change(self):
        self.on_change()
        self.session_status.publish()

    def summarize(self):
        return self.registry.summarize(self.summary_suffix)

    def load(self, names=None):
        servers = {name: conf for name, conf in self.config["servers"].items() if names is None or name in names}
        loads = asyncio.run(load_servers(servers, self.config.get("startup_timeout") or DEFAULT_STARTUP_TIMEOUT,
                                         self.config["timeout"]))
        for load in loads:
            self.registry.record_load(load)
            if not load.ok:
                logger.warning(load.failure.describe(load.name, load.transport)
                               + "".join(f"\n    {line}" for line in self.mask_output(load.name, load.stderr_tail)))
        return loads

    def mask_output(self, name, lines):
        """Hide the configured secrets a server may echo in its output."""
        return mask_secrets(lines, collect_secret_values(self.config, self.config["servers"].get(name) or {}))

    def format_error_report(self, name):
        entry = self.registry.get(name)
        return None if entry is None else format_error_report(entry, self.mask_output(name, entry.stderr_tail))

    def read_console_output(self, name):
        entry = self.registry.get(name)
        return self.mask_output(name, entry.stderr_tail[-CONSOLE_STDERR_LINES:]) if entry else []

    def start(self):
        """Load and register the healthy servers while retries wait; False, unconnected, when none is healthy or the run was stopped."""
        with self._retry_lock:
            if self.cancelled.is_set():
                return False
            self.session_status.start()
            self.load()
            if self.cancelled.is_set():
                return False
            for entry in self.registry.list_by_status(FAILED):
                self.notify_server(entry.message)
            if not self.registry.list_by_status(HEALTHY):
                self.notify_error(self.summarize())
                return False
            self.link = start_socket_connection(self.config, self.registry, self.notify_status, self.notify_error,
                                                self.handle_server_failure)
            self.session_status.publish()
            self.notify_server(self.summarize())
            for entry in self.registry.list_by_status(FAILED):
                self.schedule_retry(entry.name)
            return True

    def halt(self):
        """End the session at once without waiting on the platform, so a Stop from the tray menu never blocks on the disconnect."""
        self._stopped = True
        self.session_status.stop()
        for name in list(self._timers):
            self.cancel_retry(name)

    def stop(self):
        self.halt()
        if self.link is not None:
            self.link.disconnect()

    def retry_failed(self, names=None, automatic=False):
        """Reload only failed servers, never touching healthy ones, and register those that recover."""
        with self._retry_lock:
            if self._stopped:
                return []
            failed = [entry.name for entry in self.registry.list_by_status(FAILED)
                      if entry.name in self.config["servers"] and (names is None or entry.name in names)]
            for name in failed:
                self.cancel_retry(name)
                if not automatic:
                    self.registry.reset_retries(name)
            if not failed:
                return []
            loads = self.load(failed)
            recovered = [load.name for load in loads if load.ok]
            if recovered and self.link is not None:
                self.link.sync(recovered)
            for load in loads:
                if load.ok:
                    self.notify_server(f'Server "{load.name}" recovered: {len(load.tools)} tools.')
                else:
                    self.schedule_retry(load.name)
            if not automatic:
                self.notify_server(self.summarize())
            return loads

    def handle_server_failure(self, name):
        """A registered server failed twice in a row to start or answer: report it, take it off a protocol 2 platform, retry it if it may recover."""
        self.notify_server(self.registry.get(name).message)
        if self.link is not None:
            self.link.push_changes()
        self.schedule_retry(name)

    def schedule_retry(self, name):
        """Retry a failure that may fix itself for as long as it lasts, slowing to the last delay as unattended supervisors do."""
        entry = self.registry.get(name)
        if self._stopped or entry is None or not entry.can_auto_retry:
            return
        if entry.retry_attempts == len(RETRY_DELAYS):
            self.notify_server(f"{entry.message} Retrying automatically every {RETRY_DELAYS[-1]} s.")
        timer = threading.Timer(calculate_retry_delay(entry), self.retry_failed, kwargs={"names": [name], "automatic": True})
        timer.daemon = True
        self.registry.record_retry_scheduled(name)
        self._timers[name] = timer
        timer.start()

    def cancel_retry(self, name):
        timer = self._timers.pop(name, None)
        if timer is not None:
            timer.cancel()
        self.registry.record_retry_unscheduled(name)
