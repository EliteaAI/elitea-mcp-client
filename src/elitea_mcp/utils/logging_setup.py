"""One logging setup per command: where its records go, at which level, and with every configured secret masked first."""
import faulthandler
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .server_loader import collect_secret_values, mask_secrets
from .server_output import SERVER_OUTPUT_LOGGER_NAME

CLI = "cli"
RUN_STDIO = "run_stdio"
RUN_SSE = "run_sse"
TRAY = "tray"
SERVE = "serve"
DAEMON = "daemon"

INFO = "info"
DEBUG = "debug"
LOG_LEVELS = (INFO, DEBUG)

CLIENT_LOGGER_NAME = "elitea_mcp"
QUIET_LOGGER_NAMES = ("httpx", "httpcore", "mcp", "urllib3", "asyncio", "anyio")
SOCKET_LOGGER_NAMES = ("socketio", "engineio", "socketio.client", "engineio.client")
FILE_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
CONSOLE_FORMAT = "%(message)s"
LOG_FILE_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 5
SERVER_OUTPUT_BACKUPS = 3
OWNER_ONLY_FILE_MODE = 0o600
OWNER_ONLY_FOLDER_MODE = 0o700
UNMASKABLE_RECORD = "<log record could not be masked>"
INSTALLED_HANDLER_MARK = "_elitea_mcp_handler"
MASKED_RECORD_MARK = "_elitea_mcp_masked"

logger = logging.getLogger(__name__)
_secrets = ()


def register_log_secrets(config):
    """Mask every value a config could leak, keeping earlier ones because records about them may still be written."""
    global _secrets
    values = set(_secrets)
    values.update(collect_secret_values(config, {}))
    servers = config.get("servers")
    for server_conf in servers.values() if isinstance(servers, dict) else ():
        if isinstance(server_conf, dict):
            values.update(collect_secret_values(config, keep_object_settings(server_conf)))
    _secrets = tuple(sorted(values, key=len, reverse=True))


def keep_object_settings(server_conf):
    """The env and headers of a server only when they are objects: the config check skips a server whose are not, so it never runs."""
    return {key: server_conf[key] for key in ("env", "headers") if isinstance(server_conf.get(key), dict)}


def forget_log_secrets():
    global _secrets
    _secrets = ()


def mask_log_text(text, secrets):
    return mask_secrets([text], secrets)[0]


def is_debug_requested(config):
    """Debug comes from "log_level" in the config, or from ELITEA_MCP_DEBUG=1 as before."""
    level = (config or {}).get("log_level")
    return os.environ.get("ELITEA_MCP_DEBUG") == "1" or (isinstance(level, str) and level.strip().lower() == DEBUG)


def apply_debug_logging(enabled):
    """Switch this client's own loggers to DEBUG and back at once; libraries keep their pinned levels either way."""
    logging.getLogger(CLIENT_LOGGER_NAME).setLevel(logging.DEBUG if enabled else logging.NOTSET)


def is_debug_logging():
    return logging.getLogger(CLIENT_LOGGER_NAME).getEffectiveLevel() <= logging.DEBUG


class SecretMaskingFilter(logging.Filter):
    """Mask a record before any handler writes it, and never let masking break the code that logged; a record already masked with the current secrets is not masked again."""

    def filter(self, record):
        secrets = _secrets
        if getattr(record, MASKED_RECORD_MARK, None) is secrets:
            return True
        try:
            mask_record(record, secrets)
        except Exception:
            record.msg, record.args = UNMASKABLE_RECORD, ()
            record.exc_info = record.exc_text = record.stack_info = None
        setattr(record, MASKED_RECORD_MARK, secrets)
        return True


def mask_record(record, secrets):
    try:
        message = record.getMessage()
    except Exception:
        message = str(record.msg)
    record.msg, record.args = mask_log_text(message, secrets), ()
    if record.exc_info:
        record.exc_text = logging.Formatter().formatException(record.exc_info)
    if record.exc_text:
        record.exc_text = mask_log_text(record.exc_text, secrets)
    if record.stack_info:
        record.stack_info = mask_log_text(record.stack_info, secrets)


class SkipFileOnlyRecords(logging.Filter):
    """Keep a record logged with extra={"console": False} off the console, which already printed the same line."""

    def filter(self, record):
        return getattr(record, "console", True)


class PrivateRotatingFileHandler(RotatingFileHandler):
    """A rotating log only its owner can read, including the file a rollover opens and one an older version left readable."""

    def _open(self):
        stream = super()._open()
        restrict_to_owner(self.baseFilename, OWNER_ONLY_FILE_MODE)
        return stream


def restrict_to_owner(path, mode):
    if os.name == "nt":
        return
    try:
        os.chmod(path, mode)
    except OSError as error:
        logger.debug(f"Could not restrict {path}: {error}")


def create_log_file_handler(log_file, max_bytes=LOG_FILE_BYTES, backup_count=LOG_FILE_BACKUPS):
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    handler = PrivateRotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    handler.setFormatter(logging.Formatter(FILE_FORMAT))
    handler.addFilter(SecretMaskingFilter())
    return handler


def create_console_handler(stream, log_format):
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(log_format))
    handler.addFilter(SecretMaskingFilter())
    handler.addFilter(SkipFileOnlyRecords())
    return handler


def build_server_output_log_path(log_file):
    path = Path(log_file)
    return path.with_name(f"{path.stem}-servers{path.suffix}")


def build_daemon_output_path(log_file):
    """Where a daemon's stray stdout and stderr go: crash dumps and anything that bypasses logging, kept out of the rotating log."""
    return Path(log_file).with_suffix(".out")


def setup_logging(mode, config=None, log_file=None):
    """Route this command's records: level, files and console, each behind the secret filter; calling it again replaces the previous setup."""
    remove_installed_handlers()
    reset_logger_levels()
    config = config if isinstance(config, dict) else {}
    if config:
        register_log_secrets(config)
    root = logging.getLogger()
    if mode == CLI:
        root.setLevel(logging.WARNING)
        install_handler(root, create_console_handler(sys.stdout, FILE_FORMAT))
        return
    pin_third_party_loggers()
    apply_debug_logging(is_debug_requested(config))
    if mode in (RUN_STDIO, RUN_SSE):
        root.setLevel(logging.WARNING)
        install_handler(root, create_console_handler(sys.stderr if mode == RUN_STDIO else sys.stdout, FILE_FORMAT))
        return
    root.setLevel(logging.INFO)
    server_output = logging.getLogger(SERVER_OUTPUT_LOGGER_NAME)
    server_output.propagate = False
    if log_file:
        install_log_files(root, server_output, log_file)
    if has_console(mode):
        console = create_console_handler(sys.stderr, CONSOLE_FORMAT)
        install_handler(root, console)
        install_handler(server_output, console)
    install_crash_hooks()
    if mode == DAEMON:
        faulthandler.enable()


def install_log_files(root, server_output, log_file):
    """Write the log files, or say once on stderr why not: a log folder this user cannot write must not stop the client."""
    handlers = []
    try:
        handlers.append(create_log_file_handler(log_file))
        handlers.append(create_log_file_handler(build_server_output_log_path(log_file), backup_count=SERVER_OUTPUT_BACKUPS))
    except OSError as error:
        for handler in handlers:
            handler.close()
        report_unwritable_log(error)
        return
    install_handler(root, handlers[0])
    install_handler(server_output, handlers[1])


def report_unwritable_log(error):
    if sys.stderr is not None:
        print(f"Could not open the log file, so nothing is written to it: {error}", file=sys.stderr)


def has_console(mode):
    """`serve` always shows its log; the tray only when started from a terminal; a daemon never, as its stderr is the .out file."""
    if mode == SERVE:
        return sys.stderr is not None
    return mode == TRAY and sys.stderr is not None and sys.stderr.isatty()


def install_handler(target, handler):
    setattr(handler, INSTALLED_HANDLER_MARK, True)
    target.addHandler(handler)


def remove_installed_handlers():
    for target in (logging.getLogger(), logging.getLogger(SERVER_OUTPUT_LOGGER_NAME)):
        for handler in [handler for handler in target.handlers if getattr(handler, INSTALLED_HANDLER_MARK, False)]:
            target.removeHandler(handler)
            handler.close()


def reset_logger_levels():
    for name in (CLIENT_LOGGER_NAME, *QUIET_LOGGER_NAMES, *SOCKET_LOGGER_NAMES):
        logging.getLogger(name).setLevel(logging.NOTSET)
    logging.getLogger(SERVER_OUTPUT_LOGGER_NAME).propagate = True


def pin_third_party_loggers():
    """Libraries keep their own level whatever this client logs: httpx would write server URLs at INFO and mcp whole tool payloads at DEBUG."""
    for name in QUIET_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in SOCKET_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.ERROR)


def install_crash_hooks():
    sys.excepthook = log_uncaught_exception
    threading.excepthook = log_uncaught_thread_exception


def log_uncaught_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))


def log_uncaught_thread_exception(args):
    if args.exc_type is SystemExit:
        return
    thread_name = args.thread.name if args.thread else "unknown"
    logger.critical(f"Uncaught exception in thread {thread_name}",
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
