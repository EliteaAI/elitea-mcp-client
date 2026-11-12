import logging
import sys

from ..config import load_config
from ..config_model import check_config, write_config_schema
from ..utils.diagnostics import DOCTOR_HINT
from ..utils.logging_setup import SERVE, setup_logging
from ..utils.preflight import PreflightError, validate_platform_config
from ..utils.server_registry import FAILED
from ..utils.server_output import flush_server_output
from ..utils.server_supervisor import ServerSupervisor, collect_skipped_servers
from ..utils.sio import PlatformConnectionError

CTRL_C_POLL_SECONDS = 1

logger = logging.getLogger(__name__)


def run_servers(log_mode=SERVE, log_file=None):
    raw_config = load_config()
    report = check_config(raw_config)
    setup_logging(log_mode, report.config, log_file)
    write_config_schema()
    if report.notable:
        show(str(report), logging.WARNING, file=sys.stderr)
    if report.config is None:
        sys.exit(1)
    try:
        config = validate_platform_config(report.config)
    except PreflightError as error:
        show(f"Cannot connect to platform: {error}\n{DOCTOR_HINT}", logging.ERROR)
        sys.exit(1)
    show("\nMcp Servers from configuration:")
    show(", ".join(config["servers"].keys()))
    show("\nConnecting to the servers...")
    supervisor = ServerSupervisor(config, collect_skipped_servers(report, raw_config),
                                  notify_error=lambda message: show(message, logging.ERROR), notify_server=show)
    try:
        started = supervisor.start()
    except PlatformConnectionError as error:
        print_server_table(supervisor)
        show(f"Cannot connect to platform: {error}\n{DOCTOR_HINT}", logging.ERROR)
        sys.exit(1)
    print_server_table(supervisor)
    if not started:
        show(DOCTOR_HINT, logging.ERROR)
        sys.exit(1)

    try:
        show(f"\nStarting MCP client...")
        while not supervisor.cancelled.wait(CTRL_C_POLL_SECONDS):
            pass
    except KeyboardInterrupt:
        show("\nExiting application...")
        supervisor.stop()
        flush_server_output()
        sys.exit(0)
    show(DOCTOR_HINT, logging.ERROR)
    supervisor.stop()
    flush_server_output()
    sys.exit(1)


def show(message, level=logging.INFO, file=None):
    """Print a line of the `serve` screen and write the same line to the log only, as the console already shows it."""
    print(message, file=file)
    if message.strip():
        logger.log(level, message.strip(), extra={"console": False})


def print_server_table(supervisor):
    """Print one row per server, then the last lines a failed server wrote, with configured secrets hidden."""
    entries = supervisor.registry.list_entries()
    name_width = max((len(entry.name) for entry in entries), default=0)
    show("\nMCP servers:")
    for entry in entries:
        show(f"  {entry.mark} {entry.name:<{name_width}}  {entry.transport:<5}  {entry.describe_status()}")
    for entry in entries:
        output = supervisor.read_console_output(entry.name) if entry.status == FAILED else []
        if output:
            show(f"\nLast output of {entry.name}:")
            show("\n".join(f"    {line}" for line in output))
