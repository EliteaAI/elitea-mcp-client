"""`elitea-mcp doctor`: check the whole local setup, one line per check; the CLI prints the report and the tray saves it."""
import asyncio
import logging
import platform
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from ..config import is_ssl_verification_enabled, load_config
from ..config_errors import ConfigError
from ..config_model import DEFAULT_STARTUP_TIMEOUT, DEFAULT_TIMEOUT, Severity, check_config
from .package_updates import fetch_update_state, get_installed_version
from .preflight import check_platform
from .self_update import detect_install_method, resolve_upgrade_plan
from .server_loader import collect_secret_values, load_servers, mask_secrets, redact_urls
from .server_registry import DECLINED, FAILED, HEALTHY, ServerEntry
from .server_supervisor import collect_skipped_servers, format_error_report
from .session_status import read_running_session

REPORT_TITLE = "ELITEA MCP doctor"
DOCTOR_HINT = "Run elitea-mcp doctor for a full check."
TRAY_DIAGNOSTICS_HINT = "Help → Run Diagnostics checks the whole setup."
REPORT_FILE_NAME = "elitea-mcp-diagnostics.txt"
UNKNOWN_VERSION = "unknown (source checkout)"
SECOND_COPY_NOTE = "servers were started a second time for this check, so a server that holds a port may fail here only"
SSE_ONLY_HINT = "Only matters for elitea-mcp run --transport sse."
DECLINED_HINT = ("Another running client, or a session that has not timed out yet, holds this name in the project. "
                 "Stop it or rename the server in config.json.")

logger = logging.getLogger(__name__)


class Status(Enum):
    OK = "✓"
    WARNING = "⚠"
    FAILED = "✗"


@dataclass(frozen=True)
class CheckResult:
    status: Status
    title: str
    detail: str = ""
    hint: str = ""


@dataclass
class DoctorContext:
    """What every check reads: the config file, read once, and where server details go."""
    config_file: Path
    error_dir: Path
    list_local_session: Callable[[], Optional[list]] = lambda: None
    raw: Optional[dict] = None
    config_error: Optional[ConfigError] = None
    config_read_error: Optional[OSError] = None
    report: object = None

    @property
    def config(self):
        return self.report.config if self.report is not None else None


def ok(title, detail="", hint=""):
    return CheckResult(Status.OK, title, detail, hint)


def warn(title, detail="", hint=""):
    return CheckResult(Status.WARNING, title, detail, hint)


def fail(title, detail="", hint=""):
    return CheckResult(Status.FAILED, title, detail, hint)


def read_config(context):
    """Parse the config once; a missing or broken file leaves report None, which the later checks report as their own failure."""
    try:
        if not context.config_file.exists():
            return
        context.raw = load_config(context.config_file)
    except ConfigError as error:
        context.config_error = error
        return
    except OSError as error:
        context.config_read_error = error
        return
    context.report = check_config(context.raw)


def check_client(context):
    method, _ = detect_install_method()
    return [check_version(context), ok(f"Python {platform.python_version()}, {describe_os()}", f"installed via {method}")]


def check_version(context):
    if not is_update_check_enabled(context):
        return ok(f"elitea-mcp {get_installed_version() or UNKNOWN_VERSION}", 'not compared with PyPI ("update_check" is off)')
    state = fetch_update_state()
    title = f"elitea-mcp {state.current or UNKNOWN_VERSION}"
    if state.error:
        return warn(title, f"could not check for updates: {state.error}")
    if state.is_update_available and not state.is_python_supported:
        return warn(title, state.describe())
    if state.is_update_available:
        return warn(title, f"latest: {state.latest} — update available",
                    f"Update from the tray, or run: {resolve_upgrade_plan().display_command}")
    return ok(title, "latest")


def is_update_check_enabled(context):
    """Read the setting as the tray does, through the config model, which accepts 0 or null for off; a config with errors falls back to the raw value."""
    if context.config is not None:
        return bool(context.config.get("update_check", True))
    return context.raw is None or bool(context.raw.get("update_check", True))


def describe_os():
    if platform.system() == "Darwin":
        return f"macOS {platform.mac_ver()[0]}"
    if platform.system() == "Windows":
        return f"Windows {platform.release()}"
    return f"{platform.system()} {platform.release()}"


def check_config_file(context):
    if context.config_read_error is not None:
        reason = context.config_read_error.strerror or context.config_read_error
        return [fail("Config file", f"cannot read {context.config_file}: {reason}", "Check that the path is a readable file.")]
    if not context.config_file.exists():
        return [fail("Config file", f"not found: {context.config_file}", "Run elitea-mcp bootstrap to create it.")]
    results = [ok("Config file found", str(context.config_file))]
    if context.config_error is not None:
        return results + [fail("Config JSON", context.config_error.summary, "Run elitea-mcp validate to see where.")]
    results.append(ok("Config JSON valid"))
    problems = [problem for problem in context.report.problems if problem.severity is not Severity.INFO]
    for problem in problems:
        mark = fail if problem.severity is Severity.ERROR else warn
        results.append(mark("Config schema", f"{problem.path}: {problem.message}"))
    if not problems:
        results.append(ok("Config schema valid"))
    return results


def check_platform_access(context):
    if context.config is None:
        return [fail("Platform", "no usable config to check")]
    path_notes = []
    run = check_platform(context.config, path_notes.append)
    results = []
    for index, check in enumerate(run.checks):
        if check.error is not None:
            results.append(fail(check.title, str(check.error)))
        elif index == 0 and path_notes:
            results.append(warn(check.title, f"has a page or endpoint path; connecting to the base address {check.detail}",
                                'Set "deployment_url" to the base address.'))
        else:
            results.append(ok(check.title, check.detail))
    if not is_ssl_verification_enabled(context.config):
        results.append(warn("SSL verification", "off (the default)",
                            'Set "ssl_verify": true unless a corporate proxy needs it off.'))
    return results


def check_servers(context):
    if context.config is None:
        return [fail("Local servers", "no usable config to check")]
    servers = context.config["servers"]
    skipped = collect_skipped_servers(context.report, context.raw)
    if not servers and not skipped:
        return [warn("Local servers", "none configured", 'Add servers under "servers" in config.json.')]
    loads = asyncio.run(load_servers(servers, context.config.get("startup_timeout") or DEFAULT_STARTUP_TIMEOUT,
                                     context.config.get("timeout") or DEFAULT_TIMEOUT))
    results = [describe_load(context, load) for load in loads]
    for name, transport, problem in skipped:
        results.append(fail(f'Local server "{name}" ({transport})', f"skipped: {problem}", "Fix it in config.json."))
    return results


def describe_load(context, load):
    title = f'Local server "{load.name}" ({load.transport})'
    if load.ok:
        tools = len(load.tools)
        mark = ok if tools else warn
        return mark(title, f"started, {tools} tool{'' if tools == 1 else 's'}")
    details = write_server_details(context, load)
    return fail(title, load.failure.reason, " ".join(part for part in (load.failure.hint, details) if part))


def write_server_details(context, load):
    """Keep the server's output out of the report but hand it to the user in the same file the tray's Servers menu opens."""
    if not load.stderr_tail:
        return ""
    entry = ServerEntry(load.name, load.transport, status=FAILED, failure=load.failure)
    secrets = collect_secret_values(context.config, context.config["servers"].get(load.name) or {})
    path = context.error_dir / f"{load.name}-error.txt"
    try:
        context.error_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(format_error_report(entry, mask_secrets(load.stderr_tail, secrets)), encoding="utf-8")
    except OSError as error:
        logger.debug(f"Could not write {path}: {error}")
        return ""
    return f"Details: {path}"


def check_environment(context):
    return ([check_port(context)] if context.config is not None else []) + check_running_client(context) + [
        ok("Logs", str(context.error_dir))]


def check_port(context):
    """Warn only: the port matters for `run --transport sse` alone, so a bad value must not fail the doctor or hide the checks after it."""
    value = context.config.get("port", 8000)
    try:
        port = int(str(value).strip())
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        return warn(f'Port "{value}"', "not a port number from 1 to 65535", SSE_ONLY_HINT)
    host = str(context.config.get("host") or "0.0.0.0")
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except socket.gaierror:
            return warn(f'Host "{host}"', "not an address this machine can listen on", SSE_ONLY_HINT)
        except OSError:
            return warn(f"Port {port} in use", "only matters for elitea-mcp run --transport sse")
    return ok(f"Port {port} free", "used by elitea-mcp run --transport sse")


def check_running_client(context):
    entries = context.list_local_session()
    session = read_running_session()
    if entries is not None:
        results = [warn("Running client", f"this tray; {SECOND_COPY_NOTE}") if entries
                   else ok("Running client", "this tray, its servers not started yet")]
        if session is not None:
            results.append(warn("Running client", "another client is also running servers with this config",
                                "Stop one of them: the second copy's servers are declined by the platform."))
        return results + describe_session(entries)
    if session is None:
        return [ok("Running client", "none")]
    results = [warn("Running client", f"another client is running servers with this config; {SECOND_COPY_NOTE}")]
    if session.problem:
        return results + [warn("Running session", session.problem)]
    connection = ok("Running session", "connected to the platform") if session.connected else \
        warn("Running session", "not connected to the platform yet (starting or reconnecting)")
    return results + [connection, *describe_session(session.entries)]


def describe_session(entries):
    """The running session's servers as the tray's Servers menu shows them."""
    results = []
    for entry in entries:
        title = f'Running session: "{entry.name}" ({entry.transport})'
        if entry.status == FAILED:
            results.append(fail(title, entry.describe_status(), entry.failure.hint))
        elif entry.status == HEALTHY and entry.registration == DECLINED:
            results.append(warn(title, entry.describe_status(), DECLINED_HINT))
        elif entry.status == HEALTHY:
            results.append(ok(title, entry.describe_status()))
        else:
            results.append(warn(title, entry.describe_status()))
    return results


CHECK_GROUPS = (
    ("Client", check_client),
    ("Config", check_config_file),
    ("Platform", check_platform_access),
    ("Local servers", check_servers),
    ("Environment", check_environment),
)


def run_checks(context, on_results=None):
    """Run the groups at once, since each waits on its own network or servers, and keep their results in this order."""
    read_config(context)
    results = []
    with ThreadPoolExecutor(max_workers=len(CHECK_GROUPS), thread_name_prefix="doctor") as pool:
        futures = [(title, pool.submit(check, context)) for title, check in CHECK_GROUPS]
        for title, future in futures:
            group = collect_group(title, future)
            results.extend(group)
            if on_results is not None:
                on_results(group)
    return results


def collect_group(title, future):
    """A check that crashes is a failure, never a silent pass."""
    try:
        return future.result()
    except Exception as error:
        logger.debug(f"{title} checks crashed", exc_info=True)
        return [fail(f"{title} checks", f"the check crashed ({type(error).__name__})")]


def format_results(results):
    """The report people paste into tickets keeps only the scheme and host of URLs, because hosted MCP servers carry secrets in the path."""
    lines = []
    for result in results:
        lines.append(f"  {result.status.value} {result.title}" + (f": {result.detail}" if result.detail else ""))
        if result.hint and result.status is not Status.OK:
            lines.append(f"      {result.hint}")
    return "\n".join(redact_urls(line) for line in lines)


def count_problems(results):
    return (sum(result.status is Status.FAILED for result in results),
            sum(result.status is Status.WARNING for result in results))


def summarize(results):
    failed, warnings = count_problems(results)
    if not failed and not warnings:
        return "All checks passed."
    parts = [f"{count} {noun}{'' if count == 1 else 's'}"
             for count, noun in ((failed, "problem"), (warnings, "warning")) if count]
    return ", ".join(parts) + "."


def format_report(results):
    return f"{REPORT_TITLE}\n{format_results(results)}\n\n{summarize(results)}\n"


def exit_code(results):
    """Only a failure fails the run; a warning such as an available update does not."""
    return 1 if any(result.status is Status.FAILED for result in results) else 0
