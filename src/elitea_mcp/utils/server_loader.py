"""Load every local MCP server on its own, so one broken server never stops or hangs the others."""
import asyncio
import email.utils
import math
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Optional

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from ..config_model import DEFAULT_STARTUP_TIMEOUT, DEFAULT_TIMEOUT

COMMAND_NOT_FOUND = "command_not_found"
EXITED = "exited"
TIMEOUT = "timeout"
UNREACHABLE = "unreachable"
AUTH = "auth"
INVALID_CONFIG = "invalid_config"
REMOTE_TRANSPORTS = ("http", "sse")
STDERR_TAIL_LINES = 20
STDERR_TAIL_BYTES = 64 * 1024
MASK = "****"
MIN_SECRET_LENGTH = 4
LOCAL_URL_PATTERN = re.compile(r"https?://\S+?(?=[.,:;)\"']*(?:\s|$))")
URL_PATTERN = re.compile(r"(https?://)(?:[^\s/@?#]*@)?([^\s/?#]+?)(?:[/?#]\S*?)?(?=[.,:;)\"']*(?:\s|$))")
AUTH_STATUS_CODES = (401, 403)
SLOW_DOWN_STATUS_CODES = (429, 503)
ON_WINDOWS = os.name == "nt"
INSTALL_HINTS = {
    "npx": "Install Node.js",
    "node": "Install Node.js",
    "npm": "Install Node.js",
    "uvx": "Install uv",
    "uv": "Install uv",
    "docker": "Install Docker",
}
PACKAGE_LAUNCHERS = frozenset({"npx", "uvx", "bunx"})
DLX_LAUNCHERS = frozenset({"pnpm", "yarn"})


@dataclass(frozen=True)
class ServerFailure:
    kind: str
    reason: str
    hint: str = ""
    retry_after: Optional[float] = None

    def describe(self, name, transport):
        """Name the server, its reason and what to do, as one line for notifications, the console and the platform."""
        prefix = f'Server "{name}"' + (f" ({transport})" if transport in REMOTE_TRANSPORTS else "")
        return f"{prefix}: {self.reason}." + (f" {self.hint}" if self.hint else "")


class ServerStartError(Exception):
    """The server could not be started or reached, as opposed to a tool that failed once it was running."""

    def __init__(self, failure: ServerFailure, stderr_tail=()):
        super().__init__(failure.reason)
        self.failure = failure
        self.stderr_tail = list(stderr_tail)


@dataclass
class ServerLoad:
    name: str
    transport: str
    tools: Optional[list] = None
    failure: Optional[ServerFailure] = None
    stderr_tail: list = field(default_factory=list)

    @property
    def ok(self):
        return self.failure is None


def read_transport(server_conf):
    return str(server_conf.get("type") or "stdio").lower()


def read_startup_timeout(server_conf, default=DEFAULT_STARTUP_TIMEOUT):
    return server_conf.get("startup_timeout") or default


async def load_servers(servers, startup_timeout=DEFAULT_STARTUP_TIMEOUT, list_timeout=DEFAULT_TIMEOUT):
    """Load all servers at once; each result says which tools a server has or why it failed."""
    return list(await asyncio.gather(*(
        load_server(name, conf, read_startup_timeout(conf, startup_timeout), list_timeout)
        for name, conf in servers.items()
    )))


async def load_server(name, server_conf, startup_timeout=DEFAULT_STARTUP_TIMEOUT, list_timeout=DEFAULT_TIMEOUT):
    """List one server's tools, returning its failure instead of raising so it cannot cancel the other servers."""
    transport = read_transport(server_conf)
    with tempfile.TemporaryFile("w+b") as errlog:
        try:
            async with open_session(server_conf, startup_timeout, errlog) as session:
                tools = await list_tools_within(session, list_timeout)
            return ServerLoad(name, transport, tools=[tool.model_dump() for tool in tools])
        except Exception as error:
            leaf = find_leaf_exception(error)
            failure = classify_failure(error, server_conf, startup_timeout)
            stderr_tail = (leaf.stderr_tail if isinstance(leaf, ServerStartError) else []) or read_stderr_tail(errlog)
    return ServerLoad(name, transport, failure=failure, stderr_tail=stderr_tail)


async def list_tools_within(session, list_timeout):
    try:
        return (await asyncio.wait_for(session.list_tools(), list_timeout)).tools
    except TimeoutError:
        raise ServerStartError(ServerFailure(TIMEOUT, f"did not list its tools within {list_timeout} s",
                                             'Increase "timeout".')) from None


@asynccontextmanager
async def open_session(server_conf, startup_timeout, errlog=None, message_handler=None):
    """Yield an initialized session; the deadline covers a remote transport's opening but only a stdio handshake, so stdio teardown still kills child processes."""
    missing = find_missing_command(server_conf)
    if missing:
        raise ServerStartError(missing)
    remote = read_transport(server_conf) in REMOTE_TRANSPORTS
    started = False
    with anyio.CancelScope(deadline=anyio.current_time() + startup_timeout if remote else math.inf) as deadline:
        try:
            async with open_streams(server_conf, errlog) as (read, write):
                async with ClientSession(read, write, message_handler=message_handler) as session:
                    with anyio.fail_after(None if remote else startup_timeout):
                        await session.initialize()
                    started = True
                    deadline.deadline = math.inf
                    yield session
        except Exception as error:
            if started:
                raise
            raise ServerStartError(classify_failure(error, server_conf, startup_timeout),
                                   read_stderr_tail(errlog)) from error
    if not started:
        raise ServerStartError(classify_failure(TimeoutError(), server_conf, startup_timeout), read_stderr_tail(errlog))


@asynccontextmanager
async def open_streams(server_conf, errlog):
    transport = read_transport(server_conf)
    if transport == "http":
        async with streamablehttp_client(server_conf["url"], server_conf.get("headers") or {}) as (read, write, _):
            yield read, write
    elif transport == "sse":
        async with sse_client(server_conf["url"], server_conf.get("headers") or {}) as (read, write):
            yield read, write
    else:
        async with stdio_client(StdioServerParameters(**server_conf), errlog=errlog or sys.stderr) as (read, write):
            yield read, write


def find_missing_command(server_conf):
    """Name a bare command missing from PATH before spawning, because Windows reports it without the file name."""
    if read_transport(server_conf) != "stdio":
        return None
    command = server_conf.get("command") or ""
    if not command or os.path.dirname(command):
        return None
    if find_command(command, {**get_default_environment(), **(server_conf.get("env") or {})}.get("PATH")):
        return None
    return describe_missing_command(command)


def find_command(command, server_path):
    """Look the command up where the SDK will: on Windows it searches this process's PATH, elsewhere the server's own."""
    return shutil.which(command) if ON_WINDOWS else shutil.which(command, path=server_path)


def describe_missing_command(command):
    install = INSTALL_HINTS.get(os.path.splitext(os.path.basename(command))[0].lower())
    hint = f"{install} or use the full path in \"command\"." if install else 'Install it or use the full path in "command".'
    return ServerFailure(COMMAND_NOT_FOUND, f'command "{command}" not found', hint)


def classify_failure(error, server_conf, startup_timeout):
    """Map an exception raised while starting a server to a failure kind, with the secret parts of any URL masked."""
    failure = identify_failure(error, server_conf, startup_timeout)
    return replace(failure, reason=mask_url_secrets(failure.reason))


def identify_failure(error, server_conf, startup_timeout):
    """Pick the failure kind by exception type rather than by message text."""
    leaf = find_leaf_exception(error)
    transport = read_transport(server_conf)
    if isinstance(leaf, ServerStartError):
        return leaf.failure
    if isinstance(leaf, FileNotFoundError) and transport == "stdio":
        return describe_missing_command(server_conf.get("command") or str(leaf.filename or ""))
    if isinstance(leaf, TimeoutError):
        return ServerFailure(TIMEOUT, f"did not respond within {startup_timeout} s", describe_timeout_hint(server_conf))
    if isinstance(leaf, httpx.HTTPStatusError):
        label = f"{leaf.response.status_code} {leaf.response.reason_phrase}".strip()
        if leaf.response.status_code in AUTH_STATUS_CODES:
            return ServerFailure(AUTH, label, 'Check the token in "headers".')
        return ServerFailure(UNREACHABLE, f"{server_conf.get('url', '')} answered {label}", 'Check "url".',
                             retry_after=read_retry_after(leaf.response))
    if isinstance(leaf, httpx.LocalProtocolError):
        return ServerFailure(INVALID_CONFIG, 'a name or value in "headers" is not valid in HTTP',
                             "Remove spaces and line breaks around it in config.json.")
    if transport in REMOTE_TRANSPORTS:
        return ServerFailure(UNREACHABLE, f"cannot reach {server_conf.get('url', '')}: {describe_connect_error(leaf)}",
                             'Check "url" and that the server is running.')
    return ServerFailure(EXITED, "exited during start", 'Check its "command", "args" and "env".')


def describe_timeout_hint(server_conf):
    """A package launcher downloads the package on its first run, inside the start timeout, so say that before blaming the server."""
    launcher = find_package_launcher(server_conf)
    if launcher:
        return (f"The first run of {launcher} may still be downloading the package: run the command once in a terminal, "
                'or increase "startup_timeout".')
    return 'Check the command/URL or increase "startup_timeout".'


def find_package_launcher(server_conf):
    if read_transport(server_conf) != "stdio":
        return None
    command = os.path.splitext(os.path.basename(server_conf.get("command") or ""))[0].lower()
    args = server_conf.get("args") or []
    if command in PACKAGE_LAUNCHERS:
        return command
    if command in DLX_LAUNCHERS and args and args[0] == "dlx":
        return f"{command} dlx"
    return None


def read_retry_after(response):
    """Seconds a 429 or 503 answer asks the client to wait before trying again (RFC 9110 Retry-After), or None."""
    value = response.headers.get("retry-after", "").strip() if response.status_code in SLOW_DOWN_STATUS_CODES else ""
    if value.isdigit():
        return float(value)
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, moment.timestamp() - time.time())


def find_leaf_exception(error):
    """Return the first exception inside nested exception groups, preferring one whose kind is known."""
    leaves = list(iterate_leaf_exceptions(error))
    known = (ServerStartError, FileNotFoundError, TimeoutError, httpx.HTTPStatusError, httpx.TransportError)
    return next((leaf for leaf in leaves if isinstance(leaf, known)), leaves[0] if leaves else error)


def iterate_leaf_exceptions(error):
    if isinstance(error, BaseExceptionGroup):
        for inner in error.exceptions:
            yield from iterate_leaf_exceptions(inner)
    else:
        yield error


def describe_connect_error(error):
    if any(isinstance(cause, ConnectionRefusedError) for cause in iterate_causes(error)):
        return "connection refused"
    return str(error).strip() or type(error).__name__


def iterate_causes(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


def replay_stderr(errlog):
    """Pass on what a server wrote during a tool call, which it printed straight to this process's stderr before."""
    output = "\n".join(read_stderr_tail(errlog, lines=None))
    if output:
        print(output, file=sys.stderr, flush=True)


def read_stderr_tail(errlog, lines=STDERR_TAIL_LINES):
    if errlog is None:
        return []
    try:
        errlog.flush()
        size = errlog.seek(0, os.SEEK_END)
        errlog.seek(max(0, size - STDERR_TAIL_BYTES))
        text = errlog.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return []
    kept = [line for line in text.splitlines() if line.strip()]
    return kept if lines is None else kept[-lines:]


def collect_secret_values(config, server_conf):
    """The configured values a server's output may echo: the platform token, env values and header values."""
    values = [config.get("auth_token"), *(server_conf.get("env") or {}).values(), *(server_conf.get("headers") or {}).values()]
    secrets = set()
    for value in values:
        if not isinstance(value, str):
            continue
        secrets.add(value)
        scheme, _, credential = value.partition(" ")
        if credential and scheme.lower() in ("bearer", "basic", "token"):
            secrets.add(credential)
    return sorted((secret for secret in secrets if len(secret) >= MIN_SECRET_LENGTH), key=len, reverse=True)


def mask_secrets(lines, secrets):
    masked = []
    for line in lines:
        for secret in secrets:
            line = line.replace(secret, MASK)
        masked.append(mask_url_secrets(line))
    return masked


def redact_urls(text):
    """Keep only the scheme and host of every URL, because hosted MCP servers often carry a secret in the path or query."""
    return URL_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}", text)


def mask_url_secrets(text):
    """For text shown on this machine: drop a URL's user and password and mask every query and fragment value."""
    return LOCAL_URL_PATTERN.sub(lambda match: mask_url(match.group(0)), text)


def mask_url(url):
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return redact_urls(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path,
                                    mask_parameters(parts.query), mask_parameters(parts.fragment)))


def mask_parameters(text):
    pairs = (part.partition("=") for part in text.split("&") if part)
    return "&".join(f"{key}={MASK}" if separator else MASK for key, separator, _ in pairs)
