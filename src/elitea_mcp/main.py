import os
import argparse
import logging
import sys
import signal
import atexit
from pathlib import Path

from elitea_mcp.commands.bootstrap_servers import bootstrap_servers
from elitea_mcp.commands.run_servers import run_servers

from .config import (
    set_bootstrap_config,
    get_bootstrap_config,
    interactive_bootstrap,
)
from .clients.elitea import Agent, Agents
from .server.mcp import run

# Configure logging - avoid stdout to prevent MCP stdio interference
def configure_logging(use_stdout=True, level=logging.WARNING):
    """Configure logging, optionally avoiding stdout for MCP stdio compatibility."""
    handlers = []
    if use_stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    else:
        # Use stderr instead of stdout to avoid MCP stdio interference
        handlers.append(logging.StreamHandler(sys.stderr))
    
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers,
        force=True  # Override any existing configuration
    )

# Default logging configuration
configure_logging(use_stdout=True)
logger = logging.getLogger(__name__)

def rotate_log_file(log_file, max_bytes=5 * 1024 * 1024, backup_count=5):
    """Rotate log file if it exceeds ``max_bytes``."""
    path = Path(log_file)
    if not path.exists() or path.stat().st_size < max_bytes:
        return

    # Remove the oldest file if it exists
    oldest = path.with_suffix(path.suffix + f".{backup_count}")
    if oldest.exists():
        try:
            oldest.unlink()
        except (PermissionError, FileNotFoundError) as e:
            logger.warning(f"Failed to delete oldest log file {oldest}: {e}")

    # Shift existing files
    for i in range(backup_count - 1, 0, -1):
        src = path.with_suffix(path.suffix + f".{i}")
        if src.exists():
            dst = path.with_suffix(path.suffix + f".{i+1}")
            try:
                src.rename(dst)
            except (PermissionError, FileNotFoundError) as e:
                logger.warning(f"Failed to rename log file {src} to {dst}: {e}")

    try:
        path.rename(path.with_suffix(path.suffix + ".1"))
    except (PermissionError, FileNotFoundError) as e:
        logger.warning(f"Failed to rename log file {path} to {path.with_suffix(path.suffix + '.1')}: {e}")


def setup_file_logger(log_file, max_bytes=5 * 1024 * 1024, backup_count=5):
    """
    Set up a rotating file handler for logging and add it to the root logger.
    
    Args:
        log_file (str): Path to the log file
        max_bytes (int): Maximum size in bytes before rotation
        backup_count (int): Number of backup files to keep
    """
    from logging.handlers import RotatingFileHandler
    
    # Create directory if it doesn't exist
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Create a rotating file handler
    file_handler = RotatingFileHandler(
        log_file, 
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8'
    )
    
    # Set formatter
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    
    # Set level
    file_handler.setLevel(logging.DEBUG)
    
    # Add to root logger
    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)
    
    # Return handler in case it needs to be removed later
    return file_handler

def main(project_id=None, application_id=None, version_id=None, transport="stdio", port=8000, 
         deployment_url=None, auth_token=None, host=None):
    # Reconfigure logging for stdio transport to avoid interference
    if transport == "stdio":
        configure_logging(use_stdout=False)
        # Set root logging level to WARNING to minimize interference
        logging.getLogger().setLevel(logging.WARNING)
        # Set the specific MCP server logger to DEBUG to see our debugging output when needed
        # To enable debug logs, set environment variable: ELITEA_MCP_DEBUG=1
        debug_enabled = os.environ.get('ELITEA_MCP_DEBUG', '0') == '1'
        if debug_enabled:
            logging.getLogger('elitea_mcp.server.mcp').setLevel(logging.DEBUG)
        else:
            logging.getLogger('elitea_mcp.server.mcp').setLevel(logging.WARNING)
        
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
    
    if not project_id:
        logger.error("Project ID is required")
        return
        
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
        
    logger.debug(f"Config: {config}")
    
    logger.info(f"Starting MCP server for project {project_id}")
    if application_id:
        logger.info(f"Using application: {application_id}" + 
              (f", version: {version_id}" if version_id else ""))
    else:
        logger.info("Using all available project agents")
    run(client, transport=transport, host=host, port=port)

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
        
        # Set up environment for the subprocess
        env = os.environ.copy()
        env['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
        env['ELITEA_MCP_DAEMON_MODE'] = 'true'  # Mark as daemon subprocess
        
        # Start subprocess in background (nohup equivalent)
        if log_file:
            # Setup logging before starting the process
            setup_file_logger(log_file)
            logger.debug(f"Set up logging to file: {log_file}")
            
            with open(log_file, 'a') as log:
                process = subprocess.Popen(
                    cmd,
                    env=env,
                    stdout=log,
                    stderr=log,
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
    os.umask(0)
    
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
        # Configure logging to file with rotation
        setup_file_logger(log_file)
        
        # Still redirect stdout/stderr to the log file
        with open(log_file, 'a+') as so:
            os.dup2(so.fileno(), sys.stdout.fileno())
        with open(log_file, 'a+') as se:
            os.dup2(se.fileno(), sys.stderr.fileno())
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
        
        return {
            'serve_pid': pid_dir / 'elitea-mcp-serve.pid',
            'tray_pid': pid_dir / 'elitea-mcp-tray.pid',
            'serve_log': log_dir / 'elitea-mcp-serve.log',
            'tray_log': log_dir / 'elitea-mcp-tray.log'
        }
    else:
        # Windows - use temp directory
        temp_dir = Path(tempfile.gettempdir()) / 'elitea-mcp'
        temp_dir.mkdir(exist_ok=True)
        return {
            'serve_pid': temp_dir / 'elitea-mcp-serve.pid',
            'tray_pid': temp_dir / 'elitea-mcp-tray.pid', 
            'serve_log': temp_dir / 'elitea-mcp-serve.log',
            'tray_log': temp_dir / 'elitea-mcp-tray.log'
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

def cli():
    parser = argparse.ArgumentParser(description='MCP Client')
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
    
    args = parser.parse_args()
    
    if args.command == "bootstrap":
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
                run_servers()
        else:
            run_servers()
            
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
                    run_tray()
        else:
            # Normal mode or daemon subprocess
            if is_daemon_subprocess:
                logger.info("Running in macOS daemon subprocess...")
            run_tray()
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