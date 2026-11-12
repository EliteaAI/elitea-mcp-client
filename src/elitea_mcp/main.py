import os
import argparse
import logging
import sys
import signal
import atexit
from pathlib import Path

from elitea_mcp.commands.bootstrap_servers import bootstrap_servers
from elitea_mcp.commands.run_servers import run_servers

from .config_errors import ConfigError
from .config import (
    set_bootstrap_config,
    get_bootstrap_config,
    get_config_file,
    interactive_bootstrap,
    load_config,
)
from .config_model import check_config, write_config_schema
from .clients.elitea import Agent, Agents, ApiDetailsRequestError
from .server.mcp import run
from .utils.diagnostics import DOCTOR_HINT, REPORT_TITLE, DoctorContext, exit_code, format_results, run_checks, summarize
from .utils.package_updates import get_installed_version
from .utils.logging_setup import (CLI, DAEMON, OWNER_ONLY_FILE_MODE, OWNER_ONLY_FOLDER_MODE, RUN_SSE, RUN_STDIO, SERVE,
                                  build_daemon_output_path, register_log_secrets, report_unwritable_log, restrict_to_owner,
                                  setup_logging)
from .utils.preflight import PreflightError, validate_platform_config
from .utils.server_loader import redact_urls


logger = logging.getLogger(__name__)


def main(project_id=None, application_id=None, version_id=None, transport="stdio", port=8000, 
         deployment_url=None, auth_token=None, host=None):
    setup_logging(RUN_STDIO if transport == "stdio" else RUN_SSE, read_config_leniently())
        
    # Use command-line arguments if provided, otherwise get configuration from bootstrap
    if deployment_url and auth_token:
        # Use command-line arguments
        if not host:
            host = "0.0.0.0"
        config = {
            "deployment_url": deployment_url,
            "auth_token": auth_token,
            "host": host,
            "port": port
        }
    else:
        # Get configuration from bootstrap
        config = get_bootstrap_config()
        deployment_url = config.get("deployment_url")
        auth_token = config.get("auth_token")
        host = config.get("host", "0.0.0.0")
        port = config.get("port", 8000)
        
        if not deployment_url or not auth_token:
            logger.error("Configuration missing. Please provide --deployment_url and --auth_token arguments or run bootstrap first.")
            return
        if transport.lower() == "sse":
            port = parse_config_port(port)
            if port is None:
                return
    
    if not project_id:
        logger.error("Project ID is required")
        return

    try:
        validated = validate_platform_config({
            "deployment_url": deployment_url,
            "auth_token": auth_token,
            "project_id": project_id,
            "ssl_verify": config.get("ssl_verify", False),
        }, logger.warning)
    except PreflightError as error:
        logger.error(str(error))
        return
    deployment_url = validated["deployment_url"]
    project_id = validated["project_id"]

    try:
        if not application_id:
            # Using project-level agents when only project_id is provided
            client = Agents(base_url=deployment_url,
                            project_id=project_id,
                            auth_token=auth_token,
                            api_extra_headers=None).agents
        else:
            # Using specific agent when application_id is provided
            client = Agent(base_url=deployment_url,
                        project_id=project_id,
                        auth_token=auth_token,
                        app_id = application_id,
                        version_id=version_id)
    except ApiDetailsRequestError as error:
        logger.error(str(error))
        return

    register_log_secrets({"auth_token": auth_token})
    logger.info(f"Starting MCP server for project {project_id} on {redact_urls(deployment_url)}")
    if application_id:
        logger.info(f"Using application: {application_id}" + 
              (f", version: {version_id}" if version_id else ""))
    else:
        logger.info("Using all available project agents")
    run(client, transport=transport, host=host, port=port)

def find_default_log_file(key):
    """The default log path, or None when its folder cannot be created, so the command still runs and logs to the console."""
    try:
        return str(get_default_paths()[key])
    except OSError as error:
        report_unwritable_log(error)
        return None


def read_config_leniently():
    """The config for choosing the log level of `run`, which must start even when the file is broken or missing."""
    try:
        return load_config()
    except (ConfigError, OSError):
        return {}


def parse_config_port(value):
    """Return the configured port as a number, or None after logging why it cannot be listened on."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        logger.error(f'"port" in config must be a number from 1 to 65535, got "{value}".')
        return None
    return port

def macos_gui_daemonize(pid_file=None, log_file=None):
    """
    macOS-compatible daemonization for GUI applications.
    Uses subprocess with exec to avoid fork safety issues with Cocoa frameworks.
    """
    import subprocess
    import sys
    
    try:
        # Build command to re-execute the tray application WITHOUT --daemon flag
        # but with daemon environment variable set
        
        # Find the elitea-mcp command
        elitea_mcp_cmd = None
        
        # Try to find elitea-mcp in PATH
        import shutil
        elitea_mcp_cmd = shutil.which('elitea-mcp')
        
        if not elitea_mcp_cmd:
            # Fallback: try to run via Python module
            python_cmd = sys.executable
            cmd = [python_cmd, '-m', 'elitea_mcp.main', 'tray']
        else:
            cmd = [elitea_mcp_cmd, 'tray']
        if log_file:
            cmd += ['--log-file', str(log_file)]
        
        # Set up environment for the subprocess
        env = os.environ.copy()
        env['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
        env['ELITEA_MCP_DAEMON_MODE'] = 'true'  # Mark as daemon subprocess
        
        # Start subprocess in background (nohup equivalent)
        if log_file:
            with open_daemon_output(log_file) as output:
                process = subprocess.Popen(
                    cmd,
                    env=env,
                    stdout=output,
                    stderr=output,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,  # Detach from parent session
                    preexec_fn=None  # Don't use preexec_fn on macOS
                )
        else:
            process = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                preexec_fn=None
            )
        
        # Write PID file with the subprocess PID
        if pid_file:
            with open(pid_file, 'w+') as f:
                f.write(str(process.pid) + '\n')
        
        logger.info(f"Tray daemon started with PID: {process.pid}")
        
        # Exit the parent process - the subprocess continues as daemon
        sys.exit(0)
        
    except Exception as e:
        logger.error(f"Failed to start macOS GUI daemon: {e}")
        return False

def open_daemon_output(log_file):
    """The owner-only file a daemon's stdout and stderr go to, apart from the rotating log that logging writes."""
    path = build_daemon_output_path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(path, "ab")
    restrict_to_owner(path, OWNER_ONLY_FILE_MODE)
    return stream


def daemonize(pid_file=None, log_file=None):
    """
    Daemonize the current process for Unix systems.
    """
    if os.name != 'posix':
        logger.error("Daemon mode is only supported on Unix systems (Linux, macOS)")
        return False
    
    try:
        # First fork
        pid = os.fork()
        if pid > 0:
            # Exit first parent
            sys.exit(0)
    except OSError as e:
        logger.error(f"Fork #1 failed: {e}")
        sys.exit(1)
    
    # Decouple from parent environment
    os.chdir("/")
    os.setsid()
    
    try:
        # Second fork
        pid = os.fork()
        if pid > 0:
            # Exit from second parent
            sys.exit(0)
    except OSError as e:
        logger.error(f"Fork #2 failed: {e}")
        sys.exit(1)
    
    # Redirect standard file descriptors
    sys.stdout.flush()
    sys.stderr.flush()
    
    # Redirect stdin, stdout, stderr to devnull or log file
    with open('/dev/null', 'r') as si:
        os.dup2(si.fileno(), sys.stdin.fileno())
    
    if log_file:
        with open_daemon_output(log_file) as output:
            os.dup2(output.fileno(), sys.stdout.fileno())
            os.dup2(output.fileno(), sys.stderr.fileno())
    else:
        with open('/dev/null', 'a+') as so:
            os.dup2(so.fileno(), sys.stdout.fileno())
        with open('/dev/null', 'a+') as se:
            os.dup2(se.fileno(), sys.stderr.fileno())
    
    # Write PID file
    if pid_file:
        pid = str(os.getpid())
        with open(pid_file, 'w+') as f:
            f.write(pid + '\n')
        
        # Set up cleanup on exit
        def cleanup():
            try:
                os.remove(pid_file)
            except:
                pass
        atexit.register(cleanup)
        
        # Handle termination signals
        def signal_handler(signum, frame):
            cleanup()
            sys.exit(0)
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    
    return True

def get_default_paths():
    """Get default paths for PID and log files based on the platform."""
    from pathlib import Path
    import tempfile
    
    if os.name == 'posix':
        # Unix systems
        if os.getuid() == 0:  # Root user
            pid_dir = Path('/var/run/elitea-mcp')
            log_dir = Path('/var/log/elitea-mcp')
        else:  # Regular user
            home = Path.home()
            pid_dir = home / '.local' / 'var' / 'run' / 'elitea-mcp'
            log_dir = home / '.local' / 'var' / 'log' / 'elitea-mcp'
        
        # Create directories if they don't exist
        pid_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        if log_dir.stat().st_uid == os.getuid():
            restrict_to_owner(log_dir, OWNER_ONLY_FOLDER_MODE)
        
        return {
            'serve_pid': pid_dir / 'elitea-mcp-serve.pid',
            'tray_pid': pid_dir / 'elitea-mcp-tray.pid',
            'serve_log': log_dir / 'elitea-mcp-serve.log',
            'tray_log': log_dir / 'elitea-mcp-tray.log',
            'update_log': log_dir / 'elitea-mcp-update.log',
            'update_job': pid_dir / 'elitea-mcp-update-job.json',
            'update_result': pid_dir / 'elitea-mcp-update-result.json',
            'update_helper': pid_dir / 'elitea-mcp-update-helper.py',
        }
    else:
        # Windows - use temp directory
        temp_dir = Path(tempfile.gettempdir()) / 'elitea-mcp'
        temp_dir.mkdir(exist_ok=True)
        return {
            'serve_pid': temp_dir / 'elitea-mcp-serve.pid',
            'tray_pid': temp_dir / 'elitea-mcp-tray.pid', 
            'serve_log': temp_dir / 'elitea-mcp-serve.log',
            'tray_log': temp_dir / 'elitea-mcp-tray.log',
            'update_log': temp_dir / 'elitea-mcp-update.log',
            'update_job': temp_dir / 'elitea-mcp-update-job.json',
            'update_result': temp_dir / 'elitea-mcp-update-result.json',
            'update_helper': temp_dir / 'elitea-mcp-update-helper.py',
        }

def bootstrap(deployment_url=None, auth_token=None, host='0.0.0.0', port=8000):
    """
    Bootstrap the client with deployment URL and authentication token.
    If parameters are not provided, runs in interactive mode.
    """
    if deployment_url is not None and auth_token is not None:
        # Non-interactive mode with command line arguments
        config = set_bootstrap_config(deployment_url, auth_token, host, port)
    else:
        # Interactive mode
        config = interactive_bootstrap()
    logger.info(f"Deployment URL: {config.get('deployment_url')}")
    
    auth_token = config.get('auth_token')
    if auth_token:
        masked_token = '*' * 8 + auth_token[-4:] if len(auth_token) > 4 else '*' * 8
        logger.info(f"Auth Token: {masked_token}")
    
    if config["deployment_url"]:
        bootstrap_servers(config)
    write_config_schema()

def validate(config_file=None):
    """Print every problem in the config file; exit 1 when any is an error, so scripts can gate on it."""
    config_file = Path(config_file).resolve() if config_file else get_config_file()
    if not config_file.exists():
        print(f"Config file not found: {config_file}\nHint: run elitea-mcp bootstrap to create it.", file=sys.stderr)
        sys.exit(1)
    try:
        report = check_config(load_config(config_file))
    except ConfigError as error:
        print(error, file=sys.stderr)
        sys.exit(1)
    write_config_schema()
    if report.problems:
        print(f"{config_file}:\n{report.format_problems(include_info=True)}", file=sys.stderr)
    if report.errors:
        print(DOCTOR_HINT, file=sys.stderr)
        sys.exit(1)
    print(f"Config file is valid: {config_file}")


def doctor(config_file=None):
    """Check the whole local setup and print one line per check as each group finishes; exit 1 when any check fails."""
    logging.getLogger().setLevel(logging.ERROR)
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    config_file = Path(config_file).resolve() if config_file else get_config_file()
    context = DoctorContext(config_file, get_default_paths()["tray_log"].parent)
    print(REPORT_TITLE, flush=True)
    results = run_checks(context, lambda group: print(format_results(group), flush=True))
    print(f"\n{summarize(results)}")
    sys.exit(exit_code(results))


def cli():
    try:
        run_cli()
    except ConfigError as error:
        print(error, file=sys.stderr)
        sys.exit(1)


def run_cli():
    setup_logging(CLI)
    parser = argparse.ArgumentParser(description='MCP Client')
    parser.add_argument('--version', action='version',
                        version=f"elitea-mcp {get_installed_version() or 'unknown (source checkout)'}")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # Main command
    main_parser = subparsers.add_parser("run", help="Run the MCP client")
    main_parser.add_argument('--project_id', type=str, help='Project ID')
    main_parser.add_argument('--app_id', type=str, help='Application ID')
    main_parser.add_argument('--version_id', type=str, help='Version ID')
    main_parser.add_argument('--transport', type=str, choices=['stdio', 'sse'],  
                             default='stdio', help='Transport type (stdio or sse)')
    main_parser.add_argument('--deployment_url', type=str, 
                             help='Deployment URL (overrides config)')
    main_parser.add_argument('--auth_token', type=str, help='Authentication token (overrides config)')
    main_parser.add_argument('--host', type=str, help='Host for SSE transport (overrides config)')
    main_parser.add_argument('--port', type=int, default=8000, help='Port to listen on (for SSE transport)')
    
    # Bootstrap command - make arguments optional
    bootstrap_parser = subparsers.add_parser("bootstrap", help="Set deployment URL and auth token")
    bootstrap_parser.add_argument('--deployment_url', type=str, help='Deployment URL')
    bootstrap_parser.add_argument('--auth_token', type=str, help='Authentication token')
    bootstrap_parser.add_argument('--host', type=str, default='0.0.0.0', help='Host for SSE transport')
    bootstrap_parser.add_argument('--port', type=int, default=8000, help='Port for SSE transport')

    serve_parser = subparsers.add_parser("serve", help="Connect to platform and makes it possible to run MCP servers on user's environment")
    serve_parser.add_argument('--daemon', action='store_true', help='Run in daemon mode (background)')
    serve_parser.add_argument('--pid-file', type=str, help='PID file location for daemon mode')
    serve_parser.add_argument('--log-file', type=str, help='Log file location for daemon mode')
    
    tray_parser = subparsers.add_parser("tray", help="Start the system tray application for easy access to MCP client features")
    tray_parser.add_argument('--daemon', action='store_true', help='Run in daemon mode (background)')
    tray_parser.add_argument('--pid-file', type=str, help='PID file location for daemon mode')
    tray_parser.add_argument('--log-file', type=str, help='Log file location for daemon mode')

    validate_parser = subparsers.add_parser("validate", help="Check the config file and list every problem; exits non-zero when any is an error")
    validate_parser.add_argument('--config', type=str, help='Config file to check (defaults to the client config file)')

    doctor_parser = subparsers.add_parser("doctor", help="Check the whole local setup (client, config, platform, local servers) and print one line per check; exits non-zero when any check fails")
    doctor_parser.add_argument('--config', type=str, help='Config file to check (defaults to the client config file)')
    
    args = parser.parse_args()
    
    if args.command == "validate":
        validate(args.config)
    elif args.command == "doctor":
        doctor(args.config)
    elif args.command == "bootstrap":
        bootstrap(
            deployment_url=args.deployment_url if hasattr(args, 'deployment_url') else None,
            auth_token=args.auth_token if hasattr(args, 'auth_token') else None,
            host=args.host if hasattr(args, 'host') else '0.0.0.0',
            port=args.port if hasattr(args, 'port') else 8000
        )
    elif args.command == "serve":
        # Handle daemon mode for serve command
        if hasattr(args, 'daemon') and args.daemon:
            default_paths = get_default_paths()
            pid_file = args.pid_file if hasattr(args, 'pid_file') and args.pid_file else default_paths['serve_pid']
            log_file = args.log_file if hasattr(args, 'log_file') and args.log_file else default_paths['serve_log']
            
            logger.info(f"Starting elitea-mcp serve in daemon mode...")
            logger.info(f"PID file: {pid_file}")
            logger.info(f"Log file: {log_file}")
            
            if daemonize(str(pid_file), str(log_file)):
                run_servers(DAEMON, str(log_file))
        else:
            run_servers(SERVE, args.log_file or find_default_log_file('serve_log'))
            
    elif args.command == "tray":
        # Fix for macOS fork safety with GUI applications
        import platform
        if platform.system() == "Darwin":
            # Set environment variable to disable fork safety check as a fallback
            os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
            
            # Set multiprocessing start method to spawn to avoid fork issues
            import multiprocessing
            try:
                multiprocessing.set_start_method('spawn', force=True)
            except RuntimeError:
                # Already set, ignore
                pass
        
        from .tray import run_tray
        
        # Check if this is a daemon subprocess (even without --daemon flag)
        is_daemon_subprocess = os.environ.get('ELITEA_MCP_DAEMON_MODE') == 'true'
        
        # Handle daemon mode for tray command
        if (hasattr(args, 'daemon') and args.daemon) and not is_daemon_subprocess:
            default_paths = get_default_paths()
            pid_file = args.pid_file if hasattr(args, 'pid_file') and args.pid_file else default_paths['tray_pid']
            log_file = args.log_file if hasattr(args, 'log_file') and args.log_file else default_paths['tray_log']
            
            logger.info(f"Starting elitea-mcp tray in daemon mode...")
            logger.info(f"PID file: {pid_file}")
            logger.info(f"Log file: {log_file}")
            
            # On macOS, GUI applications have fork safety issues with traditional daemonization
            # Use alternative approach for macOS
            if platform.system() == "Darwin":
                logger.info("Using macOS-compatible daemon mode for GUI applications...")
                macos_gui_daemonize(str(pid_file), str(log_file))
                # This will exit, so the code below won't run
            else:
                # Traditional Unix daemonization for non-macOS systems
                if daemonize(str(pid_file), str(log_file)):
                    run_tray(log_file=str(log_file), daemon=True)
        else:
            # Normal mode or daemon subprocess
            if is_daemon_subprocess:
                logger.info("Running in macOS daemon subprocess...")
            run_tray(log_file=args.log_file, daemon=is_daemon_subprocess)
    elif args.command == "run" or args.command is None:
        main(
            project_id=args.project_id if hasattr(args, 'project_id') else None,
            application_id=args.app_id if hasattr(args, 'app_id') else None,
            version_id=args.version_id if hasattr(args, 'version_id') else None,
            transport=args.transport if hasattr(args, 'transport') else "stdio",
            port=args.port if hasattr(args, 'port') else 8000,
            deployment_url=args.deployment_url if hasattr(args, 'deployment_url') else None,
            auth_token=args.auth_token if hasattr(args, 'auth_token') else None,
            host=args.host if hasattr(args, 'host') else None
        )
    else:
        parser.print_help()

if __name__ == "__main__":
    cli()