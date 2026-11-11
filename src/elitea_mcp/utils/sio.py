import asyncio
import json
import logging
import tempfile
import threading
import time
from typing import Callable, Any, Dict, Iterable, List

import socketio

from ..config import is_ssl_verification_enabled
from ..config_model import DEFAULT_STARTUP_TIMEOUT
from .client_identity import EPOCH, load_client_id, take_next_generation
from .server_loader import (ServerStartError, find_leaf_exception, load_servers, open_session, read_startup_timeout,
                            mask_url_secrets, read_transport, redact_urls, replay_stderr)
from .server_registry import FAILED, HEALTHY
from .session_manager import get_session_manager
from .tool_result import build_error_result, render_tool_result

CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY = 2
REGISTRATION_TIMEOUT = 30
SYNC_ATTEMPTS = 3
RESYNC_DELAY = 60
PROTOCOL = 2
DECLINED_NOTIFICATION_SUFFIX = " - declined."
MAX_LOGGED_NAME_CHARS = 200
CALL_OK = "ok"
CALL_TOOL_ERROR = "tool_error"
CALL_START_FAILED = "start_failed"
CALL_FAILED = "failed"
CALL_SERVER_FAILED = "server_failed"
CALL_NOT_RUNNING = "not_running"

logger = logging.getLogger(__name__)


class PlatformConnectionError(Exception):
    """The platform could not be reached or refused to register the local MCP servers."""


class RegistrationTimeout(PlatformConnectionError):
    """The platform did not answer a registration in time, so it may still apply it."""


def _sanitize_server_tools(all_tools: Any) -> List[Dict[str, Any]]:
    """
    Normalize and sanitize server/tool definitions into a predictable structure.

    Expected input shape (loosely):
        [
            {
                "name": <str>,
                "tools": [
                    {
                        "name": <str>,
                        "description": <str>,
                        "inputSchema": {
                            "type": <str>,
                            "properties": <dict>,
                            "required": <list>
                        }
                    },
                    ...
                ]
            },
            ...
        ]

    Returns a list of sanitized servers with guaranteed keys and safe defaults.
    """
    sanitized_servers: List[Dict[str, Any]] = []

    # Ensure we have an iterable of servers; otherwise treat as empty.
    if not isinstance(all_tools, Iterable) or isinstance(all_tools, (str, bytes)):
        all_tools = []

    for server in all_tools:
        # Skip invalid server objects
        if not isinstance(server, dict):
            continue

        sanitized_server: Dict[str, Any] = {
            "name": server.get("name", "") or "",
            "tools": [],
        }

        tools = server.get("tools") or []
        if not isinstance(tools, Iterable) or isinstance(tools, (str, bytes)):
            tools = []

        for tool in tools:
            # Skip invalid tool objects
            if not isinstance(tool, dict):
                continue

            raw_input_schema = tool.get("inputSchema") or {}
            if not isinstance(raw_input_schema, dict):
                raw_input_schema = {}

            properties = raw_input_schema.get("properties") or {}
            if not isinstance(properties, dict):
                properties = {}

            required = raw_input_schema.get("required") or []
            if not isinstance(required, list):
                # Best effort: wrap non-list in a list
                required = [required]

            input_schema = {
                "type": raw_input_schema.get("type", "object") or "object",
                "properties": properties,
                "required": required,
            }

            sanitized_tool = {
                "name": tool.get("name", "") or "",
                "description": tool.get("description", "") or "",
                "inputSchema": input_schema,
            }
            sanitized_server["tools"].append(sanitized_tool)

        sanitized_servers.append(sanitized_server)

    return sanitized_servers


def start_socket_connection(config, registry, notify_status: Callable[[str], None] = None,
                            notify_error: Callable[[str], None] = None, on_server_failed: Callable[[str], None] = None):
    """Connect to the platform and register the registry's healthy servers, raising PlatformConnectionError when that fails."""
    link = PlatformLink(config, registry, notify_status, notify_error, on_server_failed)
    link.connect()
    return link


class PlatformLink:
    """The socket to the platform and the snapshots of this client's servers that are sent over it."""

    def __init__(self, config, registry, notify_status=None, notify_error=None, on_server_failed=None):
        self.config = config
        self.registry = registry
        self.notify_status = notify_status or _ignore_message
        self.notify_error = notify_error or _ignore_message
        self.on_server_failed = on_server_failed or _ignore_message
        self.startup_timeout = config.get("startup_timeout") or DEFAULT_STARTUP_TIMEOUT
        self.client_id = load_client_id()
        self.platform_protocol = None
        self.registered = False
        self._state_lock = threading.Lock()
        self._sending = False
        self._dirty = False
        self._pending_names = None
        self._reregistering = False
        self._resync_timer = None
        self._update_failing = False
        self.sio = create_socket_client(config)
        session_manager = get_session_manager()
        session_manager.timeout = config['timeout']
        session_manager.startup_timeout = self.startup_timeout
        session_manager.on_tools_changed = self.on_tools_changed
        self.sio.on("connect", self.on_connect)
        self.sio.on("disconnect", self.on_disconnect)
        self.sio.on("mcp_tools_list", self.on_mcp_tools_list)
        self.sio.on("mcp_tools_call", self.on_mcp_tools_call)
        self.sio.on("mcp_notification", self.on_mcp_notification)
        self.sio.on("mcp_ping", self.on_mcp_ping)

    @property
    def connected(self):
        return self.sio.connected

    def connect(self):
        _connect_with_retries(self.sio, self.config, self.notify_status)
        try:
            self.send_snapshot()
        except PlatformConnectionError:
            self.sio.disconnect()
            raise
        self.registered = True
        logger.info("Connected to platform")
        self.notify_status("Connected to platform")
        threading.Thread(target=self.sio.wait, daemon=True).start()

    def disconnect(self):
        if self._resync_timer is not None:
            self._resync_timer.cancel()
        self.sio.disconnect()

    def build_snapshot(self, names=None):
        """The whole state on a protocol 2 platform; elsewhere only the named servers, without a generation so they are only added."""
        additive = names is not None and self.platform_protocol != PROTOCOL
        healthy = [entry for entry in self.registry.list_by_status(HEALTHY) if not additive or entry.name in names]
        identity = {} if additive else {"client_id": self.client_id, "epoch": EPOCH, "generation": take_next_generation()}
        toolkit_configs = [{**toolkit, "timeout_tools_call": self.calculate_call_budget(toolkit["name"])}
                           for toolkit in _sanitize_server_tools([{"name": entry.name, "tools": entry.tools} for entry in healthy])]
        return {
            "project_id": self.config["project_id"],
            "toolkit_configs": toolkit_configs,
            "timeout_tools_list": self.config['timeout'],
            "timeout_tools_call": max((toolkit["timeout_tools_call"] for toolkit in toolkit_configs),
                                      default=get_session_manager().calculate_stateless_call_budget({})),
            "protocol": PROTOCOL,
            **identity,
            "failed_servers": [
                {"name": entry.name, "transport": entry.transport, "kind": entry.failure.kind, "message": redact_urls(entry.message)}
                for entry in self.registry.list_by_status(FAILED) if entry.name
            ],
        }

    def calculate_call_budget(self, name):
        """Per server, so the platform waits as long as this client may take; an older platform reads only the largest."""
        return get_session_manager().calculate_call_budget(self.config.get("servers", {}).get(name) or {})

    def send_snapshot(self, names=None):
        snapshot = self.build_snapshot(names)
        try:
            ack = self.sio.call("mcp_connect", snapshot, timeout=REGISTRATION_TIMEOUT)
        except socketio.exceptions.TimeoutError:
            raise RegistrationTimeout(f"The platform did not confirm the registration within {REGISTRATION_TIMEOUT} seconds.") from None
        self.apply_ack(ack, [toolkit["name"] for toolkit in snapshot["toolkit_configs"]])

    def apply_ack(self, ack, sent_names):
        if not isinstance(ack, dict):
            self.registry.mark_all_registered(sent_names)
            return
        if ack.get("ok") is False:
            raise PlatformConnectionError(ack.get("message") or f"The platform refused the connection: {ack.get('error')}")
        self.platform_protocol = ack.get("protocol")
        if ack.get("stale"):
            return
        self.registry.apply_registration(sent_names, set(ack.get("registered") or []), ack.get("declined") or [],
                                         keep_own_names=self.platform_protocol != PROTOCOL)

    def sync(self, names=None):
        """Send the current state, merging requests that arrive while a send is in flight into the next one."""
        with self._state_lock:
            if self._dirty:
                self._pending_names = None if self._pending_names is None or names is None else self._pending_names | set(names)
            else:
                self._pending_names = None if names is None else set(names)
            self._dirty = True
            if self._sending:
                return
            self._sending = True
        try:
            while True:
                with self._state_lock:
                    if not self._dirty:
                        self._sending = False
                        return
                    names, self._dirty = self._pending_names, False
                self.send_with_resend(names)
        except BaseException:
            with self._state_lock:
                self._sending = False
            raise

    def send_with_resend(self, names):
        """Resend with a new generation when an answer times out; only a failed re-registration after a reconnect drops the socket."""
        connection_sid = self.sio.get_sid()
        error = None
        for _ in range(SYNC_ATTEMPTS):
            try:
                self.send_snapshot(names)
                self.announce_connected()
                return
            except RegistrationTimeout as timeout:
                error = timeout
            except PlatformConnectionError as refusal:
                error = refusal
                break
            except socketio.exceptions.SocketIOError:
                return
        if self.sio.get_sid() != connection_sid:
            return
        if self._reregistering:
            logger.warning(f"Failed to register on platform: {error}")
            self.notify_error(str(error))
            self.sio.disconnect()
            return
        self.schedule_resync(names, error)

    def schedule_resync(self, names, error):
        """Keep the socket, so the platform keeps the last state it accepted, and try the update again later."""
        logger.warning(f"Failed to update the platform: {error}")
        if not self._update_failing:
            self._update_failing = True
            self.notify_status(f"Could not update the platform: {error} Retrying in {RESYNC_DELAY} s.")
        if self._resync_timer is not None:
            self._resync_timer.cancel()
        self._resync_timer = threading.Timer(RESYNC_DELAY, self.sync, args=(names,))
        self._resync_timer.daemon = True
        self._resync_timer.start()

    def push_changes(self):
        """Tell a protocol 2 platform about a change found during the session; an older one cannot remove or update a server."""
        if self.platform_protocol == PROTOCOL:
            threading.Thread(target=self.sync, name="mcp-snapshot", daemon=True).start()

    def on_tools_changed(self, name, tools):
        if self.platform_protocol == PROTOCOL and self.registry.record_tools(name, tools):
            self.push_changes()

    def announce_connected(self):
        self._update_failing = False
        if self._reregistering:
            self._reregistering = False
            logger.info("Connected to platform")
            self.notify_status("Connected to platform")

    def on_connect(self):
        if self.registered:
            self.registry.forget_registrations()
            self._reregistering = True
            self.sio.start_background_task(self.sync)

    def on_disconnect(self, reason):
        if reason == self.sio.reason.CLIENT_DISCONNECT:
            logger.info("Disconnected from platform")
            self.notify_status("Disconnected from platform")
        elif reason == self.sio.reason.SERVER_DISCONNECT:
            logger.warning("Disconnected by the platform")
            self.notify_error("Disconnected by the platform")
        else:
            logger.warning("Connection lost, reconnecting...")
            self.notify_status("Connection lost, reconnecting...")
        session_manager = get_session_manager()
        try:
            session_manager.cleanup_all()
            logger.info("Cleaned up persistent sessions")
        except Exception as e:
            logger.warning(f"Error during session cleanup: {e}")

    def on_mcp_tools_list(self, data):
        """Answer the legacy pull with the servers that load now; nothing on the platform sends it any more."""
        loads = asyncio.run(load_servers(self.config["servers"], self.startup_timeout, self.config["timeout"]))
        return {
            "project_id": self.config["project_id"],
            "toolkit_configs": _sanitize_server_tools([{"name": load.name, "tools": load.tools} for load in loads if load.ok]),
            "timeout_tools_list": self.config['timeout'],
            "timeout_tools_call": self.config['timeout']
        }

    def on_mcp_tools_call(self, data):
        """Always answer, so a broken server costs the agent a named error instead of the platform's whole timeout; log one line per call."""
        started = time.monotonic()
        data = data if isinstance(data, dict) else {}
        name = data.get("server")
        params = data.get("params") or {}
        status, result = CALL_FAILED, None
        try:
            status, result = self.answer_tool_call(name, params)
            return result
        finally:
            log_tool_call(name, params, status, started, result)

    def answer_tool_call(self, name, params):
        server_conf = self.config["servers"].get(name)
        if server_conf is None:
            return CALL_NOT_RUNNING, build_error_result(f'Server "{name}" is not running in this MCP client.')
        entry = self.registry.get(name)
        if entry is not None and entry.status == FAILED:
            return CALL_SERVER_FAILED, build_error_result(redact_urls(entry.message))
        try:
            result = _mcp_tools_call_sync(server_conf, params, server_name=name,
                                          startup_timeout=read_startup_timeout(server_conf, self.startup_timeout))
        except ServerStartError as error:
            message = error.failure.describe(name, read_transport(server_conf))
            logger.warning(mask_url_secrets(message))
            if self.registry.record_transport_failure(name, error.failure, error.stderr_tail):
                self.on_server_failed(name)
            return CALL_START_FAILED, build_error_result(redact_urls(message))
        except Exception as error:
            leaf = find_leaf_exception(error)
            message = (f'Server {render_log_name(name)}: tool {render_log_name(params.get("name"))} failed: '
                       f'{str(leaf) or type(leaf).__name__}')
            logger.warning(mask_url_secrets(message))
            return CALL_FAILED, build_error_result(redact_urls(message))
        self.registry.record_call_success(name)
        return (CALL_TOOL_ERROR if isinstance(result, dict) and result.get("isError") else CALL_OK), result

    def on_mcp_notification(self, notification):
        declined = isinstance(notification, str) and notification.endswith(DECLINED_NOTIFICATION_SUFFIX)
        logger.log(logging.WARNING if declined else logging.INFO, f"Platform Notification: {notification}")
        if declined:
            server_name = notification[:-len(DECLINED_NOTIFICATION_SUFFIX)]
            self.notify_status(
                f'Server "{server_name}" was declined: a server with this name is already registered in project '
                f'{self.config["project_id"]} (another running client, or a session that has not timed out yet).'
            )

    def on_mcp_ping(self, data):
        return True


def log_tool_call(name, params, status, started, result):
    """One line per call with names only: arguments and results are user data that masking cannot recognise."""
    duration_ms = round((time.monotonic() - started) * 1000)
    logger.info(f"Tool call: server={render_log_name(name)} tool={render_log_name(params.get('name'))} "
                f"status={status} duration_ms={duration_ms}")
    if logger.isEnabledFor(logging.DEBUG):
        arguments = params.get("arguments")
        logger.debug(f"Tool call: argument keys={sorted(map(str, arguments)) if isinstance(arguments, dict) else []} "
                     f"result chars={len(str(result)) if result is not None else 0}")


def render_log_name(value):
    """A server or tool name as quoted JSON of bounded length, so a name sent by the platform cannot forge log lines."""
    return json.dumps(str(value)[:MAX_LOGGED_NAME_CHARS])


def create_socket_client(config):
    if is_ssl_verification_enabled(config):
        logger.info("SSL verification enabled for SocketIO connection")
        return socketio.Client()
    logger.info("SSL verification disabled for SocketIO connection")
    return socketio.Client(ssl_verify=False, logger=False, engineio_logger=False)


def _connect_with_retries(sio, config, notify_status):
    """Open the socket, giving up after a few attempts so a wrong address fails instead of retrying silently forever."""
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            sio.connect(config["deployment_url"], headers={
                'Authorization': f"Bearer {config['auth_token']}"},
                wait_timeout=30)
            return
        except socketio.exceptions.ConnectionError as error:
            last_error = error
            if attempt < CONNECT_ATTEMPTS:
                notify_status(f"Cannot connect to platform ({error}), retrying (attempt {attempt + 1}/{CONNECT_ATTEMPTS})...")
                time.sleep(CONNECT_RETRY_DELAY * attempt)
    logger.error(f"Failed to connect to platform: {last_error}")
    raise PlatformConnectionError(f"Could not connect to {config['deployment_url']} after {CONNECT_ATTEMPTS} attempts: {last_error}")


def _ignore_message(message: str):
    """Stand in for a notification callback the caller did not provide."""


def _mcp_tools_call_sync(server_conf, params, server_name=None, startup_timeout=DEFAULT_STARTUP_TIMEOUT):
    """Synchronous wrapper for MCP tool calls that handles both stateful and stateless sessions."""
    session_manager = get_session_manager()
    
    # Check if this server should use stateful sessions
    if session_manager.is_stateful(server_conf) and server_name:
        # Use persistent session with recovery
        try:
            result = session_manager.call_tool_with_recovery_sync(server_name, server_conf, params)
            return result
        except (TimeoutError, ServerStartError):
            raise
        except Exception as e:
            logger.warning(f"Failed to call tool with stateful session: {e}. Falling back to stateless session.")
            # Fall through to stateless mode as fallback
    
    # Use stateless session (original behavior) via async wrapper
    async def _stateless_call():
        return await _mcp_tools_call(server_conf, params, startup_timeout, server_name)
    
    # Run in session manager's event loop to avoid conflicts
    return session_manager._run_in_loop(_stateless_call(), timeout=session_manager.calculate_stateless_call_budget(
        {**server_conf, "startup_timeout": startup_timeout}))


async def _mcp_tools_call(server_conf, params, startup_timeout=DEFAULT_STARTUP_TIMEOUT, server_name=None):
    """Start the server for one call, as servers that are not stateful are not kept running."""
    with tempfile.TemporaryFile("w+b") as errlog:
        try:
            async with open_session(server_conf, startup_timeout, errlog) as session:
                tool_result = await asyncio.wait_for(session.call_tool(params["name"], params["arguments"]),
                                                     get_session_manager().timeout)
                return render_tool_result(tool_result)
        finally:
            replay_stderr(errlog, server_name)
