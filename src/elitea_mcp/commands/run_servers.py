import sys

from ..config import load_config
from ..config_model import check_config, write_config_schema
from ..utils.preflight import PreflightError, validate_platform_config
from ..utils.server_registry import FAILED
from ..utils.server_supervisor import ServerSupervisor, collect_skipped_servers
from ..utils.sio import PlatformConnectionError


def run_servers():
    raw_config = load_config()
    report = check_config(raw_config)
    write_config_schema()
    if report.notable:
        print(report, file=sys.stderr)
    if report.config is None:
        sys.exit(1)
    try:
        config = validate_platform_config(report.config)
    except PreflightError as error:
        print(f"Cannot connect to platform: {error}")
        sys.exit(1)
    print("\nMcp Servers from configuration:")
    print(", ".join(config["servers"].keys()))
    print("\nConnecting to the servers...")
    supervisor = ServerSupervisor(config, collect_skipped_servers(report, raw_config), notify_error=print,
                                  notify_server=print)
    try:
        started = supervisor.start()
    except PlatformConnectionError as error:
        print_server_table(supervisor)
        print(f"Cannot connect to platform: {error}")
        sys.exit(1)
    print_server_table(supervisor)
    if not started:
        sys.exit(1)

    try:
        print(f"\nStarting MCP client...")
        while True:
            user_input = input("").strip()
            
    except KeyboardInterrupt:
        print("\nExiting application...")
        supervisor.stop()
        sys.exit(0)


def print_server_table(supervisor):
    """Print one row per server, then the last lines a failed server wrote, with configured secrets hidden."""
    entries = supervisor.registry.list_entries()
    name_width = max((len(entry.name) for entry in entries), default=0)
    print("\nMCP servers:")
    for entry in entries:
        print(f"  {entry.mark} {entry.name:<{name_width}}  {entry.transport:<5}  {entry.describe_status()}")
    for entry in entries:
        output = supervisor.read_console_output(entry.name) if entry.status == FAILED else []
        if output:
            print(f"\nLast output of {entry.name}:")
            print("\n".join(f"    {line}" for line in output))
