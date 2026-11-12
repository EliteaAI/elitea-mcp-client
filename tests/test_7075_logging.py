"""#7075: logs reach the file, never carry a configured secret, and are reachable from the tray."""
import json
import logging
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from elitea_mcp import config as config_module
from elitea_mcp import main as main_module
from elitea_mcp import tray as tray_module
from elitea_mcp.commands import run_servers as run_servers_module
from elitea_mcp.config_errors import parse_config
from elitea_mcp.config_model import check_config
from elitea_mcp.server import mcp as mcp_server
from elitea_mcp.utils import logging_setup, server_output, server_supervisor as supervisor_module, sio as sio_module
from elitea_mcp.utils.diagnostics import DoctorContext, check_environment
from elitea_mcp.utils.logging_setup import (CLI, DAEMON, RUN_STDIO, SERVE, TRAY, UNMASKABLE_RECORD, SecretMaskingFilter,
                                            build_server_output_log_path, is_debug_logging, register_log_secrets,
                                            setup_logging)
from elitea_mcp.utils.server_loader import (MASK, ServerFailure, ServerLoad, ServerStartError, collect_secret_values,
                                            mask_secrets, mask_url_secrets, redact_urls)
from elitea_mcp.utils.preflight import PreflightError
from elitea_mcp.utils.server_output import ServerOutputPipe, flush_server_output
from elitea_mcp.utils.server_registry import ServerRegistry
from elitea_mcp.utils.server_supervisor import ServerSupervisor
from elitea_mcp.utils.session_manager import get_session_manager
from tests import fixtures_7076 as fixtures

TOKEN = "elitea-token-7075-SECRET"
GITHUB_PAT = "ghp_7075/needs\"escaping"
HEADER_KEY = "header-key-7075-SECRET"
QUERY_SECRET = "query-secret-7075"
DSN_PASSWORD = "dsn-password-7075"
PEM = "-----BEGIN KEY-----\nfirst-line-7075-SECRET\nsecond-line-7075-SECRET\n-----END KEY-----"
CONFIG = {
    "auth_token": TOKEN,
    "servers": {
        "github": {"command": "npx", "env": {"GITHUB": GITHUB_PAT, "KEY": PEM}},
        "remote": {"type": "http", "url": f"https://mcp.example.com/mcp?api_key={QUERY_SECRET}",
                   "headers": {"Authorization": f"Bearer {HEADER_KEY}"}},
        "mode": {"command": "npx", "env": {"MODE": "https"}},
    },
}
SECRETS = [TOKEN, GITHUB_PAT, HEADER_KEY, QUERY_SECRET, DSN_PASSWORD, "first-line-7075-SECRET", "second-line-7075-SECRET"]


def read_all_logs(folder):
    return "".join(path.read_text(encoding="utf-8", errors="replace") for path in Path(folder).rglob("*") if path.is_file())


def find_leaked_secrets(text):
    return [secret for secret in SECRETS if secret in text]


def log_every_kind_of_record():
    client = logging.getLogger("elitea_mcp.utils.sio")
    client.debug(f"Config: {CONFIG}")
    client.info(f"connecting with Bearer {HEADER_KEY} to postgresql://user:{DSN_PASSWORD}@db:5432/app")
    client.info(f"GET https://mcp.example.com/mcp?api_key={QUERY_SECRET} failed")
    client.info("json %s, url-encoded %s", json.dumps({"pat": GITHUB_PAT}), "ghp_7075%2Fneeds%22escaping")
    try:
        try:
            raise ValueError(f"inner {TOKEN}")
        except ValueError as inner:
            raise RuntimeError(f"outer {repr(PEM)}") from inner
    except RuntimeError:
        client.exception("tool call crashed")
    logging.getLogger("httpx").error(f"HTTP Request: GET https://mcp.example.com/mcp?api_key={QUERY_SECRET} 500")
    logging.getLogger("mcp.client.streamable_http").warning(f"server said {TOKEN}")
    server_output.forward_server_output("github", [f"npm notice token={TOKEN}", PEM])
    assert flush_server_output()


def test_no_configured_secret_reaches_any_log_file_even_with_debug_on(tmp_path):
    setup_logging(SERVE, {**CONFIG, "log_level": "debug"}, tmp_path / "logs" / "elitea-mcp-serve.log")

    log_every_kind_of_record()

    written = read_all_logs(tmp_path / "logs")
    assert "tool call crashed" in written and "npm notice" in written
    assert find_leaked_secrets(written) == []


def test_the_console_of_serve_is_masked_as_well(capfd):
    setup_logging(SERVE, CONFIG)

    log_every_kind_of_record()

    assert find_leaked_secrets("".join(capfd.readouterr())) == []


def test_a_library_writing_a_server_url_at_info_never_reaches_the_file(tmp_path):
    setup_logging(SERVE, {"log_level": "debug"}, tmp_path / "serve.log")

    logging.getLogger("httpx").info('HTTP Request: GET https://h/s/path-secret-7075/mcp "HTTP/1.1 200 OK"')
    logging.getLogger("mcp.client.streamable_http").info("Received session ID: session-7075")

    written = read_all_logs(tmp_path)
    assert "path-secret-7075" not in written and "session-7075" not in written


def list_unmasked_handlers():
    loggers = [logging.getLogger(), *(logger for logger in logging.Logger.manager.loggerDict.values()
                                      if isinstance(logger, logging.Logger))]
    return {(logger.name, id(handler)) for logger in loggers for handler in logger.handlers
            if not isinstance(handler, logging.NullHandler) and not type(handler).__module__.startswith("_pytest")
            and not any(isinstance(f, SecretMaskingFilter) for f in handler.filters)}


def test_the_client_adds_no_handler_that_skips_masking(tmp_path):
    before = list_unmasked_handlers()

    setup_logging(SERVE, CONFIG, tmp_path / "serve.log")
    sio_module.create_socket_client({"ssl_verify": True})
    sio_module.create_socket_client({"ssl_verify": False})

    assert list_unmasked_handlers() - before == set()


def test_a_masking_failure_never_reaches_the_code_that_logged(tmp_path, monkeypatch):
    setup_logging(SERVE, CONFIG, tmp_path / "serve.log")
    monkeypatch.setattr(logging_setup, "mask_log_text", MagicMock(side_effect=RuntimeError("bug")))

    logging.getLogger("elitea_mcp.utils.sio").info(f"Tool call with {TOKEN}")

    written = read_all_logs(tmp_path)
    assert UNMASKABLE_RECORD in written and TOKEN not in written


def test_a_configured_https_value_does_not_unmask_a_query_secret():
    secrets = collect_secret_values(CONFIG, CONFIG["servers"]["mode"])

    masked = mask_secrets([f"Client error for url https://mcp.example.com/mcp?api_key={QUERY_SECRET}"], secrets)

    assert QUERY_SECRET not in masked[0] and "api_key=" in masked[0]


def test_a_password_in_a_url_of_any_scheme_is_dropped():
    line = f"input: 'postgresql://user:{DSN_PASSWORD}@db:5432/app' and mongodb+srv://u:{DSN_PASSWORD}@cluster/x"

    assert DSN_PASSWORD not in mask_url_secrets(line)
    assert DSN_PASSWORD not in redact_urls(line)
    assert "postgresql://db:5432/app" in mask_url_secrets(line)


def test_a_url_without_a_host_does_not_hide_a_later_url_from_redaction():
    assert "token-after" not in redact_urls("see https://?,http://host.example/x?t=token-after")


@pytest.mark.parametrize("text", ["http://a" + "." * 100_000 + "x", "a." * 100_000 + "://x", "x://" * 50_000,
                                  "https://" + "u:p@" * 50_000])
def test_masking_a_long_hostile_line_takes_linear_time(text):
    secrets = collect_secret_values(CONFIG, CONFIG["servers"]["github"])
    started = time.perf_counter()
    mask_url_secrets(text)
    redact_urls(text)
    mask_secrets([text], secrets)
    assert time.perf_counter() - started < 1.0


def test_secret_values_are_also_masked_escaped_encoded_and_line_by_line():
    secrets = collect_secret_values({"auth_token": None}, CONFIG["servers"]["github"])
    lines = [json.dumps({"pat": GITHUB_PAT}), repr(GITHUB_PAT), "ghp_7075%2Fneeds%22escaping", "second-line-7075-SECRET"]

    assert all(MASK in line for line in mask_secrets(lines, secrets))
    assert find_leaked_secrets(" ".join(mask_secrets(lines, secrets))) == []


def test_each_record_is_written_once_by_a_daemon(tmp_path):
    setup_logging(DAEMON, CONFIG, tmp_path / "elitea-mcp-serve.log")

    logging.getLogger("elitea_mcp.utils.sio").warning("only-once-7075")

    assert (tmp_path / "elitea-mcp-serve.log").read_text().count("only-once-7075") == 1
    assert not any(isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
                   for handler in logging.getLogger().handlers if getattr(handler, logging_setup.INSTALLED_HANDLER_MARK, False))


def test_the_log_files_and_the_default_folder_are_private_but_a_custom_folder_is_left_alone(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    handler = logging_setup.create_log_file_handler(shared / "custom.log", max_bytes=200, backup_count=2)
    logger = logging.getLogger("elitea_mcp.test_7075_rotation")
    logger.addHandler(handler)
    try:
        for index in range(20):
            logger.warning(f"line {index} " + "x" * 50)
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert stat.S_IMODE(shared.stat().st_mode) == 0o755
    assert {stat.S_IMODE(path.stat().st_mode) for path in shared.iterdir()} == {0o600}
    log_dir = main_module.get_default_paths()["tray_log"].parent
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700


def test_a_line_the_serve_screen_already_printed_goes_to_the_file_only(tmp_path, capfd):
    setup_logging(SERVE, None, tmp_path / "serve.log")

    logging.getLogger("elitea_mcp.commands.run_servers").info("table-row-7075", extra={"console": False})
    logging.getLogger("elitea_mcp.utils.sio").info("Connected to platform")

    console = capfd.readouterr().err
    assert "table-row-7075" not in console and console.strip() == "Connected to platform"
    assert "table-row-7075" in (tmp_path / "serve.log").read_text()


def test_the_tray_without_a_console_still_logs_to_its_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    setup_logging(TRAY, None, tmp_path / "tray.log")

    logging.getLogger("elitea_mcp.tray").info("Server Status: Connected to platform")

    assert "Server Status: Connected to platform" in (tmp_path / "tray.log").read_text()


def test_the_cli_commands_keep_their_quiet_output(capsys):
    setup_logging(SERVE, {"log_level": "debug"})
    setup_logging(CLI)
    logging.getLogger().setLevel(logging.ERROR)

    logging.getLogger("elitea_mcp.utils.package_updates").warning("New version 9.9 of elitea-mcp is available")
    logging.getLogger("httpx").warning("noise")

    assert capsys.readouterr() == ("", "")
    assert logging.getLogger("elitea_mcp").level == logging.NOTSET


def test_run_over_stdio_keeps_stdout_for_the_protocol(capfd):
    setup_logging(RUN_STDIO, {"log_level": "DEBUG"})

    logging.getLogger("elitea_mcp.server.mcp").debug("debug-for-run-7075")

    out, err = capfd.readouterr()
    assert out == "" and "debug-for-run-7075" in err


def test_run_starts_with_a_broken_config_and_logs_at_its_default_level(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text('{"log_level": "debug",', encoding="utf-8")
    monkeypatch.setattr(main_module, "load_config", lambda: config_module.load_config(config_file))

    assert main_module.read_config_leniently() == {}


def test_run_never_logs_the_token_it_was_given(monkeypatch, capfd):
    monkeypatch.setenv("ELITEA_MCP_DEBUG", "1")
    monkeypatch.setattr(main_module, "read_config_leniently", lambda: {})
    monkeypatch.setattr(main_module, "validate_platform_config", lambda config, notify: config)
    monkeypatch.setattr(main_module, "Agents", MagicMock())
    monkeypatch.setattr(main_module, "run", MagicMock())

    main_module.main(project_id=2, deployment_url="https://dev.example.com", auth_token=TOKEN)

    assert TOKEN not in "".join(capfd.readouterr())


def test_debug_logs_of_run_describe_an_agent_answer_by_its_shape_only(caplog):
    caplog.set_level(logging.DEBUG, logger="elitea_mcp")
    response = {"chat_history": [{"role": "user", "content": "private question 7075"}],
                "thinking_steps": ["private thought 7075"], "error": None}

    mcp_server.log_response_shape(response, "private answer 7075")

    assert "chat history messages: 1" in caplog.text
    assert "private" not in caplog.text


def build_link(config_servers, registry=None):
    link = sio_module.PlatformLink.__new__(sio_module.PlatformLink)
    link.config = {"servers": config_servers}
    link.registry = registry or ServerRegistry()
    link.startup_timeout = 5
    link.on_server_failed = MagicMock()
    return link


def find_tool_call_lines(caplog):
    return [record.getMessage() for record in caplog.records if record.getMessage().startswith("Tool call: server=")]


@pytest.mark.parametrize("outcome, status", [
    ("text", "ok"),
    ({"isError": True, "content": [{"type": "text", "text": "no"}]}, "tool_error"),
    (ServerStartError(ServerFailure("exited", "exited during start")), "start_failed"),
    (ValueError("boom"), "failed"),
])
def test_each_tool_call_writes_one_line_with_its_status(outcome, status, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    effect = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
    monkeypatch.setattr(sio_module, "_mcp_tools_call_sync", MagicMock(**effect))
    link = build_link({"github": {"command": "npx"}})

    link.on_mcp_tools_call({"server": "github", "params": {"name": "search", "arguments": {}}})

    lines = find_tool_call_lines(caplog)
    assert len(lines) == 1
    assert lines[0].startswith('Tool call: server="github" tool="search" ') and f" status={status} " in lines[0]
    assert "duration_ms=" in lines[0]


def test_a_call_to_a_failed_or_unknown_server_is_logged_with_its_own_status(caplog):
    caplog.set_level(logging.INFO)
    registry = ServerRegistry()
    registry.record_load(ServerLoad("broken", "stdio", failure=ServerFailure("exited", "exited during start")))
    link = build_link({"broken": {"command": "npx"}}, registry)

    link.on_mcp_tools_call({"server": "broken", "params": {"name": "x"}})
    link.on_mcp_tools_call({"server": "nowhere", "params": {"name": "x"}})

    statuses = [line.split(" status=")[1].split()[0] for line in find_tool_call_lines(caplog)]
    assert statuses == ["server_failed", "not_running"]


def test_a_hostile_tool_name_cannot_forge_a_log_line_and_arguments_are_never_logged(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(sio_module, "_mcp_tools_call_sync", MagicMock(return_value="done"))
    link = build_link({"github": {"command": "npx"}})
    hostile = "x\n2026-01-01 - elitea_mcp - ERROR - forged\x1b[31m" + "y" * 5000

    link.on_mcp_tools_call({"server": "github", "params": {"name": hostile, "arguments": {"password": "hunter2-7075"}}})

    line = find_tool_call_lines(caplog)[0]
    assert "\n" not in line and "\x1b" not in line and len(line) < 400
    assert "hunter2-7075" not in caplog.text and "password" in caplog.text


def test_a_stateful_server_output_reaches_the_server_log_live(tmp_path):
    setup_logging(SERVE, None, tmp_path / "serve.log")
    server = {**fixtures.healthy(tmp_path), "stateful": True}

    try:
        result = sio_module._mcp_tools_call_sync(server, {"name": "echo", "arguments": {"text": "stateful-7075"}},
                                                 server_name="kept")
        assert result == "stateful-7075"
        deadline = time.monotonic() + 5
        while "stateful-7075" not in read_all_logs(tmp_path) and time.monotonic() < deadline:
            flush_server_output()
            time.sleep(0.05)
    finally:
        get_session_manager().cleanup_session_sync("kept")

    servers_log = build_server_output_log_path(tmp_path / "serve.log").read_text()
    assert "[kept] echo server log: stateful-7075" in servers_log


def test_the_pipe_keeps_every_line_a_busy_server_writes(tmp_path):
    setup_logging(SERVE, None, tmp_path / "serve.log")
    pipe = ServerOutputPipe("busy")
    child = subprocess.Popen([sys.executable, "-c", "import sys\nfor i in range(3000): print(f'line {i}', file=sys.stderr)"],
                             stderr=pipe.writer)
    pipe.close_writer()
    child.wait(timeout=30)

    deadline = time.monotonic() + 10
    while read_all_logs(tmp_path).count("[busy] line ") < 3000 and time.monotonic() < deadline:
        flush_server_output()
        time.sleep(0.05)
    assert read_all_logs(tmp_path).count("[busy] line ") == 3000


class BlockingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.messages = []

    def emit(self, record):
        self.release.wait(10)
        self.messages.append(record.getMessage())


def test_a_stalled_log_never_blocks_the_server_and_dropped_lines_are_counted(monkeypatch):
    monkeypatch.setattr(server_output, "QUEUE_LINES", 3)
    forwarder = server_output.ServerOutputForwarder()
    handler = BlockingHandler()
    output_logger = logging.getLogger(server_output.SERVER_OUTPUT_LOGGER_NAME)
    output_logger.addHandler(handler)
    output_logger.setLevel(logging.INFO)
    try:
        started = time.monotonic()
        for index in range(50):
            forwarder.submit("noisy", f"line {index}")
        assert time.monotonic() - started < 1.0
        handler.release.set()
        assert forwarder.flush(5)
        forwarder.submit("noisy", "after the stall")
        assert forwarder.flush(5)
    finally:
        output_logger.removeHandler(handler)
        output_logger.setLevel(logging.NOTSET)

    assert any("lines dropped" in message for message in handler.messages)
    assert handler.messages[-1] == "[noisy] after the stall"


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "get_config_file", lambda: path)
    monkeypatch.setattr(tray_module, "get_config_file", lambda: path)
    return path


def build_tray_app(tmp_path):
    app = tray_module.MCPTrayApp.__new__(tray_module.MCPTrayApp)
    app.icon = None
    app._log_file = tmp_path / "logs" / "elitea-mcp-tray.log"
    app.update_menu = MagicMock()
    app.show_error = MagicMock()
    app._open_file = MagicMock()
    return app


def test_debug_logging_switches_at_once_and_keeps_every_other_key(config_file, tmp_path):
    original = '{\n  "$schema": "./config.schema.json",\n  "auth_token": "t",\n  "mcpServers": {"a": {"command": "npx"}}\n}'
    config_file.write_text(original, encoding="utf-8")
    app = build_tray_app(tmp_path)

    app.toggle_debug_logging()

    assert is_debug_logging()
    saved = json.loads(config_file.read_text())
    assert list(saved) == ["$schema", "auth_token", "mcpServers", "log_level"] and saved["log_level"] == "debug"
    assert stat.S_IMODE(config_module.backup_file_for(config_file).stat().st_mode) == 0o600
    app.toggle_debug_logging()
    assert not is_debug_logging() and json.loads(config_file.read_text())["log_level"] == "info"


def test_the_switch_writes_nothing_when_the_level_does_not_change_and_seeds_a_new_file(config_file):
    tray_module.save_log_level("info")
    assert json.loads(config_file.read_text()) == {"$schema": "./config.schema.json", "log_level": "info"}
    written = config_file.stat().st_mtime_ns
    time.sleep(0.01)

    tray_module.save_log_level("info")

    assert config_file.stat().st_mtime_ns == written


def test_the_switch_is_offered_only_when_a_rewrite_cannot_lose_anything(config_file, tmp_path, monkeypatch):
    monkeypatch.setattr(tray_module, "get_server_controller", MagicMock(return_value=MagicMock(daemon_mode=False)))
    monkeypatch.setattr(tray_module, "UpdateChecker", MagicMock())
    monkeypatch.setattr(tray_module, "get_default_paths", lambda: {"tray_log": tmp_path / "tray.log"})
    config_file.write_text('{"servers": {"a": {"command": "npx"}, "a": {"command": "uvx"}}}', encoding="utf-8")
    app = tray_module.MCPTrayApp()

    help_items = next(item for item in app.get_menu_items() if item.text == "Help").submenu.items

    assert [item.text for item in help_items][:4] == ["Run Diagnostics", "Open Log File", "Open Logs Folder", "Debug Logging"]
    assert not help_items[3].enabled
    assert tray_module.has_duplicate_keys(parse_config('{"a": {"b": 1, "b": 2}}', config_file))


def test_open_log_file_opens_the_tray_log_even_before_anything_was_written(tmp_path):
    app = build_tray_app(tmp_path)

    app.open_log_file()
    app.open_logs_folder()

    assert app._open_file.call_args_list[0].args == (app.log_file,)
    assert app._open_file.call_args_list[1].args == (app.log_file.parent,)
    assert stat.S_IMODE(app.log_file.stat().st_mode) == 0o600


def test_an_unknown_log_level_is_a_warning_and_info_is_used():
    report = check_config({"log_level": "verbose", "servers": {}})

    assert report.config["log_level"] == "info"
    assert [problem.severity.value for problem in report.problems if problem.path == "log_level"] == ["warning"]


def test_the_doctor_names_the_logs_folder(tmp_path):
    results = check_environment(DoctorContext(tmp_path / "config.json", tmp_path / "logs"))

    assert any(result.title == "Logs" and result.detail == str(tmp_path / "logs") for result in results)


def test_a_record_that_cannot_format_its_arguments_is_still_written(tmp_path):
    register_log_secrets(CONFIG)
    handler = logging_setup.create_log_file_handler(tmp_path / "own.log")
    own_logger = logging.getLogger("elitea_mcp.test_7075_bad_format")
    own_logger.propagate = False
    own_logger.addHandler(handler)
    try:
        own_logger.warning(f"bad %d format {TOKEN}", "not-a-number")
    finally:
        own_logger.removeHandler(handler)
        handler.close()

    written = (tmp_path / "own.log").read_text()
    assert "bad %d format" in written and TOKEN not in written


def test_httpx_status_error_text_from_a_remote_server_is_masked(tmp_path):
    setup_logging(SERVE, CONFIG, tmp_path / "serve.log")
    request = httpx.Request("GET", f"https://mcp.example.com/mcp?api_key={QUERY_SECRET}")
    error = httpx.HTTPStatusError("Client error '401' for url " + str(request.url), request=request,
                                  response=httpx.Response(401, request=request))

    logging.getLogger("elitea_mcp.utils.session_manager").warning(f"Error calling tool on attempt 1: {error}")

    assert QUERY_SECRET not in read_all_logs(tmp_path)


def test_a_record_masked_before_a_secret_was_registered_is_masked_again():
    record = logging.LogRecord("elitea_mcp.utils.sio", logging.INFO, __file__, 1, f"token {TOKEN}", None, None)
    SecretMaskingFilter().filter(record)

    register_log_secrets(CONFIG)
    SecretMaskingFilter().filter(record)

    assert TOKEN not in record.getMessage()


def test_what_serve_prints_when_it_cannot_start_is_also_in_its_log(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_servers_module, "load_config", lambda: {"servers": {"github": {"command": "npx"}}})
    monkeypatch.setattr(run_servers_module, "write_config_schema", MagicMock())
    monkeypatch.setattr(run_servers_module, "validate_platform_config", MagicMock(side_effect=PreflightError("bad token")))

    with pytest.raises(SystemExit):
        run_servers_module.run_servers(SERVE, str(tmp_path / "serve.log"))

    assert capsys.readouterr().out.count("Cannot connect to platform: bad token") == 1
    assert "ERROR - Cannot connect to platform: bad token" in (tmp_path / "serve.log").read_text()


def test_each_loaded_server_is_logged_with_its_tool_count(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    loads = [ServerLoad("github", "stdio", tools=[{"name": "a"}, {"name": "b"}])]
    monkeypatch.setattr(supervisor_module, "load_servers", MagicMock(return_value=loads))
    monkeypatch.setattr(supervisor_module.asyncio, "run", lambda value: value)

    ServerSupervisor({"servers": {"github": {"command": "npx"}}, "timeout": 5}).load()

    assert 'Server "github" (stdio): loaded 2 tools' in caplog.text


def test_an_exception_no_thread_catches_is_written_to_the_log(tmp_path):
    setup_logging(SERVE, CONFIG, tmp_path / "serve.log")

    worker = threading.Thread(target=lambda: (_ for _ in ()).throw(RuntimeError(f"crashed with {TOKEN}")), name="worker")
    worker.start()
    worker.join()

    written = (tmp_path / "serve.log").read_text()
    assert "Uncaught exception in thread worker" in written and "RuntimeError: crashed with" in written
    assert TOKEN not in written


def test_a_daemon_sends_its_raw_output_to_a_private_out_file_and_tells_the_tray_child_where_to_log(tmp_path, monkeypatch):
    started = {}

    def fake_popen(cmd, **kwargs):
        started.update(cmd=cmd, stdout=kwargs["stdout"].name)
        return MagicMock(pid=4242)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/elitea-mcp")
    log_file = tmp_path / "elitea-mcp-tray.log"

    with pytest.raises(SystemExit):
        main_module.macos_gui_daemonize(str(tmp_path / "tray.pid"), str(log_file))

    assert started["cmd"] == ["/usr/local/bin/elitea-mcp", "tray", "--log-file", str(log_file)]
    assert started["stdout"] == str(tmp_path / "elitea-mcp-tray.out")
    assert stat.S_IMODE((tmp_path / "elitea-mcp-tray.out").stat().st_mode) == 0o600


@pytest.mark.parametrize("value", ["https://api.telegram.org/botSECRETTOKEN123/sendMessage?chat_id=1",
                                   "postgresql://user:p4ssword@db.internal/SECRETDB?sslmode=require",
                                   "https://host/SECRETPATHTOKEN?"])
def test_a_configured_value_that_is_a_url_is_masked_whole(value):
    secrets = collect_secret_values({}, {"env": {"WEBHOOK": value}})

    assert mask_secrets([f"posting to {value}, done"], secrets) == ["posting to ****, done"]


@pytest.mark.parametrize("mode", [TRAY, SERVE, DAEMON])
def test_a_log_file_that_cannot_be_opened_does_not_stop_the_client(mode, tmp_path, capfd):
    unwritable = tmp_path / "elitea-mcp.log"
    unwritable.touch()
    unwritable.chmod(0)

    setup_logging(mode, {}, unwritable)
    logging.getLogger("elitea_mcp.utils.sio").warning("still running")

    assert "Could not open the log file" in capfd.readouterr().err


def test_serve_and_the_tray_start_when_the_default_log_folder_cannot_be_created(monkeypatch, capfd):
    monkeypatch.setattr(main_module, "get_default_paths", MagicMock(side_effect=PermissionError("read-only home")))
    monkeypatch.setattr(tray_module, "get_default_paths", MagicMock(side_effect=PermissionError("read-only home")))
    monkeypatch.setattr(tray_module, "get_server_controller", MagicMock(return_value=MagicMock(daemon_mode=False)))
    monkeypatch.setattr(tray_module, "UpdateChecker", MagicMock())

    assert main_module.find_default_log_file("serve_log") is None
    app = tray_module.MCPTrayApp(None)
    app.show_error = MagicMock()
    app.open_log_file()

    assert "read-only home" in capfd.readouterr().err
    assert "read-only home" in app.show_error.call_args.args[1]


def test_a_password_holding_an_at_sign_is_dropped_whole_from_a_connection_string():
    secrets = collect_secret_values({}, {"env": {"DB_PASSWORD": "p@ssw0rd"}})

    assert mask_secrets(["postgresql://admin:p@ssw0rd@db:5432/x"], secrets) == ["postgresql://db:5432/x"]
    assert mask_url_secrets("postgresql://admin:not@configured@db:5432/x") == "postgresql://db:5432/x"
    assert mask_url_secrets("mongodb://h/db mail x@y.z ssh://host/x@y") == "mongodb://h/db mail x@y.z ssh://host/x@y"


@pytest.mark.parametrize("config", [{"servers": {"a": {"command": "x", "env": ["A=B"]}}},
                                    {"servers": {"a": {"type": "http", "headers": "Bearer x"}}},
                                    {"servers": [{"command": "x"}]}, [1], "text"])
def test_a_config_of_the_wrong_shape_still_starts_the_tray_and_run(config, tmp_path):
    setup_logging(TRAY, config, tmp_path / "tray.log")
    setup_logging(RUN_STDIO, config)

    logging.getLogger("elitea_mcp.tray").warning("started")


@pytest.mark.parametrize("password, line, expected", [
    ("p@ss/word", "postgresql://admin:p@ss/word@db/app", "postgresql://db/app"),
    ("p?ss", "https://admin:p?ss@host/x", "https://host/x"),
    ("p#ss1", "https://admin:p#ss1@host/x", "https://host/x"),
])
def test_a_password_with_url_characters_is_masked_whole_inside_a_url(password, line, expected):
    assert mask_secrets([line], collect_secret_values({}, {"env": {"PW": password}})) == [expected]


def test_a_configured_base_url_does_not_hide_a_query_secret_from_masking():
    secrets = collect_secret_values({}, {"env": {"API_URL": "https://api.example.com"}})

    assert mask_secrets(["GET https://api.example.com/v1?api_key=UNCONFIGURED1 failed"], secrets) == [
        "GET ****/v1?api_key=**** failed"]


@pytest.mark.parametrize("value, line, unconfigured", [
    ("correct horse battery", "GET https://api.example.com/v1?pw=correct horse battery failed", None),
    ("ab&cd=ef", "https://h/?k=ab&cd=ef", None),
    ("https", "GET https://api.example.com/v1?api_key=UNCONFIGURED1 failed", "UNCONFIGURED1"),
    ("go https", "go https://api.example.com/v1?api_key=UNCONFIGURED1", "UNCONFIGURED1"),
    ("//cdn.example.com", "https://cdn.example.com/x?t=UNCONFIGURED1", "UNCONFIGURED1"),
    ("https://h/x?token=abcd", "posting https://h/x?token=abcd done", None),
    ("mongodb+srv://u:pw@cluster/db", "connecting mongodb+srv://u:pw@cluster/db now", None),
])
def test_a_configured_value_is_masked_whole_without_hiding_a_url_from_masking(value, line, unconfigured):
    masked = mask_secrets([line], collect_secret_values({}, {"env": {"VALUE": value}}))[0]

    assert value not in masked and all(word not in masked for word in value.split() if len(word) > 5)
    assert unconfigured is None or unconfigured not in masked


def test_replacing_an_earlier_secret_does_not_shift_which_values_are_held_back():
    secrets = collect_secret_values({"auth_token": "long-secret-value-7075"}, {"env": {"MODE": "https"}})

    masked = mask_secrets(["token=long-secret-value-7075 mode=https url=https://h/x?api_key=UNCONFIGURED1"], secrets)[0]

    assert "long-secret-value-7075" not in masked and "UNCONFIGURED1" not in masked


def test_an_error_the_supervisor_reports_during_serve_is_logged_as_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_servers_module, "load_config", lambda: {"servers": {"github": {"command": "npx"}}})
    monkeypatch.setattr(run_servers_module, "write_config_schema", MagicMock())
    monkeypatch.setattr(run_servers_module, "validate_platform_config", lambda config: config)
    supervisor_class = MagicMock()
    supervisor_class.return_value.start.return_value = False
    supervisor_class.return_value.registry.list_entries.return_value = []
    monkeypatch.setattr(run_servers_module, "ServerSupervisor", supervisor_class)

    with pytest.raises(SystemExit):
        run_servers_module.run_servers(SERVE, str(tmp_path / "serve.log"))
    supervisor_class.call_args.kwargs["notify_error"]("Disconnected by the platform")

    assert capsys.readouterr().out.count("Disconnected by the platform") == 1
    assert "ERROR - Disconnected by the platform" in (tmp_path / "serve.log").read_text()


def test_the_switch_refuses_to_rewrite_a_file_with_duplicate_keys_even_when_the_menu_is_stale(config_file, tmp_path):
    original = '{"servers": {"a": {"command": "npx"}, "a": {"command": "uvx"}}}'
    config_file.write_text(original, encoding="utf-8")
    app = build_tray_app(tmp_path)

    app.toggle_debug_logging()

    assert config_file.read_text() == original
    assert not is_debug_logging()
    assert "duplicate keys" in app.show_error.call_args.args[1]
