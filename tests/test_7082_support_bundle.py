"""`elitea-mcp support-bundle` and tray Help → Export Support Bundle (#7082)."""
import base64
import io
import json
import logging
import os
import re
import stat
import sys
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from rich.logging import RichHandler

from src.elitea_mcp import main as main_module
from src.elitea_mcp import tray as tray_module
from src.elitea_mcp.config_errors import list_hidden_values, redact_values
from src.elitea_mcp.config_model import ClientConfig, RemoteServer, StdioServer
from src.elitea_mcp.utils import logging_setup, support_bundle
from src.elitea_mcp.utils.diagnostics import REPORT_FILE_NAME, DoctorContext, fail, ok
from src.elitea_mcp.utils.server_loader import list_secret_forms
from src.elitea_mcp.utils.support_bundle import (CONFIG_RULES, CREDENTIAL_PATTERNS, DAEMON_OUTPUT_BUDGET, MAIN_LOG_BUDGET,
                                                 REPORTS_BUDGET, SERVER_LOG_BUDGET, SERVER_RULES, UPDATE_LOG_BUDGET,
                                                 LogFiles, build_support_bundle, mask_args, resolve_output)

SUFFIX = '/+"\\é'
CORE = re.compile(rb"Cnry[0-9a-f]{8}")


def canary(index, suffix=True):
    return f"Cnry{index:08x}" + (SUFFIX if suffix else "")


TOKEN = canary(1)
BEARER = canary(2)
HEADER = canary(3)
ENV_CREDENTIAL = canary(4)
ENV_NEUTRAL = canary(5)
ARG_FLAG = canary(6, False)
ARG_EQUALS = canary(7, False)
ARG_SHORT = canary(8, False)
ARG_HEADER = canary(9, False)
ARG_POSITIONAL = canary(10, False)
DSN_PASSWORD = canary(11, False)
URL_QUERY = canary(12, False)
URL_PATH = canary(13, False)
URL_USERINFO = canary(14, False)
UNKNOWN_TOP = canary(15)
UNKNOWN_SERVER = canary(16)
LEGACY_ENV = canary(17)
ARG_JSON = canary(18, False)
ENV_JSON_INNER = canary(19, False)
WRONG_SHAPE_ENV = canary(20, False)
WRONG_SHAPE_HEADERS = canary(21, False)
WRONG_SHAPE_ARGS = canary(22, False)
WRONG_SHAPE_SERVER = canary(23, False)
ARG_ENV_ASSIGNMENT = canary(24, False)
WRONG_SHAPE_ARG_ITEM = canary(33, False)
ENV_URL_PATH = canary(25, False)
OLD_JWT = "eyJhbGciOiJIUzUxMiJ9.eyJzdWIiOiJDbnJ5MDAwMDAwMWEifQ.Cnry0000001a"
OLD_BEARER = canary(27, False)
OLD_GITHUB_PAT = "ghp_" + canary(28, False) + "AAAAAAAAAAAA"
OLD_BASIC_PASSWORD = canary(29, False)
LOG_ONLY_URL_PATH = canary(30, False)
OLD_KEY_VALUE = canary(31, False)
SURVIVOR_LINE = "Tool call github.search_code ok in 120 ms"


def canary_config(home):
    return {
        "deployment_url": "https://elitea.example.com",
        "auth_token": TOKEN,
        "project_id": 13,
        "port": 3000,
        "api_key": UNKNOWN_TOP,
        "servers": {
            "github": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github", "--api-key", ARG_FLAG, f"--token={ARG_EQUALS}",
                         f"-p{ARG_SHORT}", "--header", f"X-Api-Key: {ARG_HEADER}", ARG_POSITIONAL,
                         "--config", json.dumps({"githubPersonalAccessToken": ARG_JSON}),
                         f"postgresql://admin:{DSN_PASSWORD}@db.local:5432/app", str(home)],
                "env": {"GITHUB_TOKEN": ENV_CREDENTIAL, "WORKSPACE": ENV_NEUTRAL, "PORT": "3000",
                        "JIRA_URL": "https://acme.atlassian.net", "MCP_TRANSPORT": "http",
                        "OPENAPI_MCP_HEADERS": json.dumps({"Authorization": f"Bearer {ENV_JSON_INNER}"})},
                "token": UNKNOWN_SERVER,
            },
            "hosted": {
                "type": "http",
                "url": f"https://admin:{URL_USERINFO}@mcp.example.com/s/{URL_PATH}/mcp?api_key={URL_QUERY}",
                "headers": {"Authorization": f"Bearer {BEARER}", "X-Api-Key": HEADER, "X-Forwarded-Proto": "https"},
            },
            "docker": {"command": "docker", "args": ["run", "-i", "--rm", "-e", "GITHUB_TOKEN", "-e",
                                                     f"API_KEY={ARG_ENV_ASSIGNMENT}", "ghcr.io/github/github-mcp-server"]},
            "listy": {"command": "npx", "env": [f"GITHUB_TOKEN={WRONG_SHAPE_ENV}"]},
            "stringy": {"type": "http", "url": "https://h.example.com", "headers": f"Authorization: {WRONG_SHAPE_HEADERS}"},
            "oneline": {"command": "npx", "args": f"-y server-foo --api-key {WRONG_SHAPE_ARGS}"},
            "broken": f"npx server-bar {WRONG_SHAPE_SERVER}",
            "dicty": {"command": "npx", "args": ["-y", {"token": WRONG_SHAPE_ARG_ITEM}]},
        },
        "mcpServers": {"legacy": {"command": "uvx", "env": {"LEGACY_SECRET": LEGACY_ENV}}},
    }


CONFIG_CANARIES = (TOKEN, BEARER, HEADER, ENV_CREDENTIAL, ENV_NEUTRAL, ARG_FLAG, ARG_EQUALS, ARG_SHORT, ARG_HEADER,
                   ARG_POSITIONAL, DSN_PASSWORD, URL_QUERY, URL_PATH, URL_USERINFO, UNKNOWN_TOP, UNKNOWN_SERVER, LEGACY_ENV,
                   ARG_JSON, ENV_JSON_INNER, WRONG_SHAPE_ENV, WRONG_SHAPE_HEADERS, WRONG_SHAPE_ARGS, WRONG_SHAPE_SERVER,
                   ARG_ENV_ASSIGNMENT, WRONG_SHAPE_ARG_ITEM)


def echo_forms(value):
    """Every way a log line may carry a value, including the ones a value list does not produce: JSON inside JSON, Unicode left unescaped, a URL keeping its slashes, bytes."""
    return {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(json.dumps(value))[3:-3],
            repr(value)[1:-1], repr(value.encode())[2:-1], urllib.parse.quote(value), urllib.parse.quote(value, safe=""),
            urllib.parse.quote_plus(value)}


def log_text(home):
    lines = [SURVIVOR_LINE, "listening on port 3000", f"cwd {home}/project"]
    for value in CONFIG_CANARIES:
        lines += [f"value {form} seen" for form in sorted(echo_forms(value))]
    basic = base64.b64encode(f"svc:{OLD_BASIC_PASSWORD}".encode()).decode()
    lines += [
        f'{{"auth_token": "{OLD_JWT}"}}', f"Connecting with auth_token={OLD_JWT}",
        f"headers={{'Authorization': 'Bearer {OLD_BEARER}'}}", f"Authorization: Bearer {OLD_BEARER}",
        f"{{'Authorization': 'Basic {basic}'}}", f"pip: using {OLD_GITHUB_PAT}", f"client_secret={OLD_KEY_VALUE}",
        f"POST https://hooks.example.com/services/{LOG_ONLY_URL_PATH}/x failed",
        f"retry https://acme.atlassian.net/share/{ENV_URL_PATH}/x",
        f"request failed for url 'https://api.example.com/bot{ENV_URL_PATH}/getMe'",
        f"[notion] token {ENV_JSON_INNER} is invalid",
        "-----BEGIN OPENSSH PRIVATE KEY-----", f"b3BlbnNzaC1rZXktdjEAAAA{canary(32, False)}", "-----END OPENSSH PRIVATE KEY-----",
    ]
    return "\n".join(lines) + "\n"


def rich_wrapped_text(message):
    """What a FastMCP server's Rich handler (its own settings, mcp/server/fastmcp/utilities/logging.py) writes into -servers.log: a long line folded at 80 columns into padded, indented records."""
    stream = io.StringIO()
    handler = RichHandler(console=Console(file=stream, width=80), rich_tracebacks=True)
    handler.emit(logging.LogRecord("x", 20, "server.py", 10, message, (), None))
    return "".join(f"2026-11-16 10:00:00,000 - elitea_mcp.server_output - INFO - [srv] {line}\n"
                   for line in stream.getvalue().splitlines())


def decoded_views(data):
    """A member as the bytes it holds and as a reader would decode them, each also twice: URL-unquoted, JSON-unescaped, base64 runs."""
    text = data.decode("utf-8", errors="replace")
    views = [text]
    for _ in range(2):
        views += [urllib.parse.unquote(views[-1]), unescape_json(views[-1])]
    for run in re.findall(r"[A-Za-z0-9+/=]{16,}", text):
        try:
            views.append(base64.b64decode(run + "=" * (-len(run) % 4)).decode("utf-8", errors="replace"))
        except ValueError:
            continue
    return [view.encode("utf-8", errors="replace") for view in views]


def unescape_json(text):
    return re.sub(r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])', lambda match: json.loads(f'"{match.group(0)}"'), text)


def find_canary_cores(members):
    return {(name, core.decode()) for name, data in members.items() for view in decoded_views(data)
            for core in CORE.findall(view)}


LOG_FILE_NAMES = ("elitea-mcp-tray.log", "elitea-mcp-tray.log.1", "elitea-mcp-tray-servers.log", "elitea-mcp-tray.out",
                  "elitea-mcp-serve.log", "elitea-mcp-serve-servers.log", "elitea-mcp-serve.out",
                  "elitea-mcp-update.log", "github-error.txt", REPORT_FILE_NAME)


@pytest.fixture
def folders(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    config_file = tmp_path / "config" / "config.json"
    config_file.parent.mkdir()
    return log_dir, config_file


def log_files_in(log_dir, own_log=None):
    return LogFiles.from_default_paths({"tray_log": log_dir / "elitea-mcp-tray.log", "serve_log": log_dir / "elitea-mcp-serve.log",
                                        "update_log": log_dir / "elitea-mcp-update.log"}, own_log)


def doctor_context(config_file, log_dir, list_local_session=lambda: None):
    return DoctorContext(config_file, log_dir, list_local_session)


@pytest.fixture
def leaky_doctor():
    """A doctor that reads the config as the real one does, and whose report echoes every canary, so doctor.txt is proven masked too."""
    results = [ok("Token", "accepted"), fail("Token", "rejected by the platform (401)"),
               ok('Local server "github" (stdio)', "started, 3 tools"), fail("Leak", " ".join(CONFIG_CANARIES))]

    def run(context, on_results=None):
        support_bundle.read_config(context)
        if on_results is not None:
            on_results(results)
        return results
    with patch.object(support_bundle, "run_checks", side_effect=run) as fake:
        yield fake


def read_members(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def write_canary_setup(log_dir, config_file):
    home = Path.home()
    home.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(canary_config(home)), encoding="utf-8")
    for name in LOG_FILE_NAMES:
        (log_dir / name).write_text(log_text(home), encoding="utf-8")
    with open(log_dir / "elitea-mcp-tray-servers.log", "a", encoding="utf-8") as stream:
        stream.write(rich_wrapped_text(f"HTTP Request: GET https://api.telegram.org/bot{ENV_URL_PATH}/getMe key {ENV_CREDENTIAL}"))


def export(config_file, log_dir, output=None, **kwargs):
    return build_support_bundle(doctor_context(config_file, log_dir), output or log_dir, log_files_in(log_dir), **kwargs)


# AC2: no secret in clear text

def test_no_canary_reaches_any_member_in_any_decoding(folders, leaky_doctor):
    log_dir, config_file = folders
    write_canary_setup(log_dir, config_file)

    members = read_members(export(config_file, log_dir))

    assert {f"logs/{name}" for name in LOG_FILE_NAMES} <= set(members)
    assert {"doctor.txt", "config.masked.json", "versions.txt", "logs/README.txt"} <= set(members)
    assert find_canary_cores(members) == set()
    assert not any(b"eyJhbGci" in data or b"ghp_" in data for data in members.values())


def test_the_plain_values_survive_the_masking(folders, leaky_doctor):
    log_dir, config_file = folders
    write_canary_setup(log_dir, config_file)

    members = read_members(export(config_file, log_dir))

    config = json.loads(members["config.masked.json"])
    assert (config["port"], config["project_id"], config["deployment_url"]) == (3000, 13, "https://elitea.example.com")
    assert set(config["servers"]) == {"github", "hosted", "docker", "listy", "stringy", "oneline", "broken", "dicty"}
    github = config["servers"]["github"]
    assert github["command"] == "npx"
    assert github["args"][:5] == ["-y", "@modelcontextprotocol/server-github", "--api-key", "****", "--token=****"]
    assert github["args"][-1] == "~"
    assert github["env"]["PORT"] == "3000" and github["env"]["GITHUB_TOKEN"] == "****"
    assert (github["token"], config["api_key"], config["auth_token"]) == ("****", "****", "****")
    assert config["servers"]["hosted"] == {"type": "http", "url": "https://mcp.example.com",
                                           "headers": {"Authorization": "****", "X-Api-Key": "****", "X-Forwarded-Proto": "****"}}
    assert config["servers"]["docker"]["args"] == ["run", "-i", "--rm", "-e", "GITHUB_TOKEN", "-e", "API_KEY=****",
                                                   "ghcr.io/github/github-mcp-server"]
    assert config["mcpServers"] == {"legacy": {"command": "uvx", "env": {"LEGACY_SECRET": "****"}}}
    assert "  ✗ Token: rejected by the platform (401)" in members["doctor.txt"].decode()
    tray_log = members["logs/elitea-mcp-tray.log"].decode()
    for survivor in (SURVIVOR_LINE, "listening on port 3000", "cwd ~/project", "https://hooks.example.com",
                     "https://api.example.com"):
        assert survivor in tray_log


def test_a_secret_folded_by_rich_across_server_output_records_is_masked_whole(folders, leaky_doctor):
    log_dir, config_file = folders
    secret = "Cnry" + "abcdef0123456789" * 4
    config_file.write_text(json.dumps({"servers": {"srv": {"command": "npx", "env": {"API_KEY": secret}}}}), encoding="utf-8")
    (log_dir / "elitea-mcp-tray-servers.log").write_text(
        rich_wrapped_text(f"GET https://api.telegram.org/bot{canary(34, False)}x{secret}/getMe key {secret}"))

    member = read_members(export(config_file, log_dir))["logs/elitea-mcp-tray-servers.log"].decode()

    assert not any(secret[index:index + 8] in member for index in range(len(secret) - 7)), member
    assert canary(34, False) not in member and "https://api.telegram.org" in member


def test_wrong_shapes_are_masked_whole(folders, leaky_doctor):
    log_dir, config_file = folders
    write_canary_setup(log_dir, config_file)

    servers = json.loads(read_members(export(config_file, log_dir))["config.masked.json"])["servers"]

    assert servers["listy"]["env"] == "****"
    assert servers["stringy"]["headers"] == "****"
    assert servers["oneline"]["args"] == "****"
    assert servers["broken"] == "****"
    assert servers["dicty"]["args"] == "****"


def test_a_broken_config_still_masks_its_secrets_in_the_logs(folders, leaky_doctor):
    log_dir, config_file = folders
    config_file.write_text('{"auth_token": "%s", "servers": {"a": {"env": {"API_KEY": "%s"}}},}' % (canary(40, False), canary(41, False)),
                           encoding="utf-8")
    (log_dir / "elitea-mcp-tray.log").write_text(f"old {canary(40, False)} and {canary(41, False)} and {canary(42, False)}\n")
    config_file.with_name("config.json.bak").write_text(json.dumps({"auth_token": canary(42, False)}), encoding="utf-8")

    members = read_members(export(config_file, log_dir))

    assert "config.masked.json" not in members
    assert b"Trailing comma" in members["config.error.txt"]
    assert {(name, core) for name, core in find_canary_cores(members) if name != "doctor.txt"} == set()


def test_the_tray_secrets_mask_a_token_rotated_away_from_the_config(folders, leaky_doctor):
    log_dir, config_file = folders
    config_file.write_text(json.dumps({"auth_token": "the-new-token-123"}), encoding="utf-8")
    (log_dir / "elitea-mcp-tray.log").write_text(f"used {canary(43, False)}\n")

    unmasked = read_members(export(config_file, log_dir))["logs/elitea-mcp-tray.log"]
    masked = read_members(export(config_file, log_dir, log_dir / "second.zip", live_secrets=(canary(43, False),)))

    assert canary(43, False).encode() in unmasked
    assert masked["logs/elitea-mcp-tray.log"] == b"used ****\n"


def test_known_misses_stay_in_clear_text(folders, leaky_doctor):
    """The accepted limits, pinned so that closing one is a deliberate change: a secret with no prefix, no keyword and no configured value; a URL path segment with no digit; a short arg value echoed alone."""
    log_dir, config_file = folders
    config_file.write_text(json.dumps({"servers": {"a": {"command": "npx", "args": ["-y", "pkg", "Cnry44x"]}}}), encoding="utf-8")
    (log_dir / "elitea-mcp-tray.log").write_text(f"using {canary(45, False)}\nGET https://h.example.com/abcdefghijkl\n"
                                                 f"POST /abcdefghijkl/x\narg Cnry44x\n")

    tray_log = read_members(export(config_file, log_dir))["logs/elitea-mcp-tray.log"].decode()

    assert canary(45, False) in tray_log
    assert "/abcdefghijkl/x" in tray_log
    assert "arg Cnry44x" in tray_log


def test_doctor_lines_are_not_touched_by_the_credential_patterns(folders, leaky_doctor):
    log_dir, config_file = folders

    doctor = read_members(export(config_file, log_dir))["doctor.txt"].decode()

    assert "✓ Token: accepted" in doctor and "✗ Token: rejected by the platform (401)" in doctor


@pytest.mark.parametrize("command, args, expected", [
    ("npx", ["-y", "@modelcontextprotocol/server-github"], ["-y", "@modelcontextprotocol/server-github"]),
    ("npx.cmd", ["-y", "@scope/pkg@1.2.3"], ["-y", "@scope/pkg@1.2.3"]),
    ("cmd", ["/c", "npx", "-y", "@modelcontextprotocol/server-filesystem", "C:/nowhere/x"],
     ["/c", "npx", "-y", "@modelcontextprotocol/server-filesystem", "****"]),
    ("C:\\Program Files\\Docker\\docker.exe", ["run", "-i", "--rm", "-e", "TOKEN_NAME", "mcp/github:latest"],
     ["run", "-i", "--rm", "-e", "TOKEN_NAME", "mcp/github:latest"]),
    ("uvx", ["--from", "git+https://github.com/x/y@main", "mcp-y"], ["--from", "git+https://github.com", "mcp-y"]),
    ("/usr/bin/python3", ["-m", "my_server", "--port", "3000", "-v", "-it"], ["-m", "my_server", "--port", "3000", "-v", "-it"]),
    ("npx", ["-y", "pkg", "--pin", "1234", "--model", "gpt-4", "-pS3cretValue"], ["-y", "pkg", "--pin", "****", "--model", "****", "-p****"]),
    ("npx", ["-y", "pkg", "deadbeefdeadbeefdeadbeef"], ["-y", "pkg", "****"]),
    ("npx", ["-y", "pkg", "--creds", "1234", "--bearer", "42"], ["-y", "pkg", "--creds", "****", "--bearer", "****"]),
    ("node", ["server.js"], ["server.js"]),
])
def test_args_keep_what_tells_a_wrong_package_apart_and_mask_the_rest(command, args, expected):
    assert mask_args(args, command, None, []) == expected


@pytest.mark.parametrize("command, args", [
    ("node", ["{script}", "my-db-password1"]),
    ("node", ["/missing/index.js", "my-db-password1"]),
    ("node", ["./index.js", "my-db-password1"]),
    ("python3", ["{script}", "--verbose", "my-db-password1"]),
    ("deno", ["run", "{script}", "my-db-password1"]),
    ("uv", ["run", "{script}", "my-db-password1"]),
    ("cmd", ["/c", "node", "{script}", "my-db-password1"]),
    ("node", ["--no-warnings", "{script}", "my-db-password1"]),
    ("deno", ["run", "--allow-net", "https://x.example.com/y.ts", "my-db-password1"]),
])
def test_a_value_after_a_script_is_never_taken_for_the_package(tmp_path, command, args):
    script = tmp_path / "index.js"
    script.write_text("")
    hidden = []

    masked = mask_args([arg.format(script=script) for arg in args], command, str(tmp_path), hidden)

    assert masked[-1] == "****" and "my-db-password1" in hidden
    assert "my-db-password1" not in json.dumps(masked)


@pytest.mark.parametrize("command, args", [
    ("uv", ["--directory", "{folder}", "run", "weather.py"]),
    ("npx", ["-y", "--registry", "https://r.example.com", "@scope/server-x"]),
    ("uvx", ["--python", "3.12", "mcp-server-time"]),
])
def test_a_launcher_option_value_does_not_cost_the_package(tmp_path, command, args):
    args = [arg.format(folder=tmp_path) for arg in args]
    hidden = []

    masked = mask_args(args, command, None, hidden)

    assert masked[-1] == args[-1] and args[-1] not in hidden


def test_an_existing_path_is_kept_and_a_missing_one_masked(tmp_path):
    (tmp_path / "srv").mkdir()

    assert mask_args([str(tmp_path / "srv"), str(tmp_path / "gone")], "node", None, []) == [str(tmp_path / "srv"), "****"]
    assert mask_args(["./srv"], "node", str(tmp_path), []) == ["./srv"]


def test_the_rule_tables_cover_exactly_the_keys_the_config_model_reads():
    def names(model):
        return {field.alias or name for name, field in model.model_fields.items()}

    assert set(CONFIG_RULES) - {"mcpServers"} == names(ClientConfig)
    assert set(SERVER_RULES) == names(StdioServer) | names(RemoteServer)


def test_list_hidden_values_finds_what_redact_values_stars_out():
    text = '{"auth_token": "t\\"ok", "port": 8000, "servers": {"a": {"args": ["--key", "v1"]}},}'

    hidden = list_hidden_values(text)

    assert hidden == [("auth_token", 't"ok'), ("args", "--key"), ("args", "v1")]
    assert redact_values(text) == '{"auth_token": "*****", "port": 8000, "servers": {"a": {"args": ["*****", "**"]}},}'


def test_the_log_filter_also_masks_legacy_mcp_servers():
    logging_setup.register_log_secrets({"mcpServers": {"a": {"env": {"API_KEY": "legacy-secret-value"}}}})

    assert "legacy-secret-value" in logging_setup.get_log_secrets()


@pytest.mark.parametrize("unit", ["eyJ", "eyJ-", "token", "a_token:", "token'", "authorization: ", "Bearer ", "sk-", "x-", "AKIA"])
def test_every_credential_pattern_stays_linear(unit):
    def measure(pattern, size):
        text = unit * (size // len(unit))
        start = time.perf_counter()
        pattern.sub("x", text)
        return time.perf_counter() - start

    for pattern in CREDENTIAL_PATTERNS:
        small, large = min(measure(pattern, 2000) for _ in range(3)), min(measure(pattern, 8000) for _ in range(3))
        assert large <= max(small * 8, 0.002), pattern.pattern
        assert measure(pattern, 1024 * 1024) < 1.0, pattern.pattern


# AC3: the size is bounded

def test_every_log_family_keeps_to_its_budget(folders, leaky_doctor):
    log_dir, config_file = folders
    config_file.write_text("{}", encoding="utf-8")
    block = "".join(f"2026-11-16 10:00:{index % 60:02d},123 - elitea_mcp.utils.sio - INFO - Tool call ok in {index} ms\n"
                    for index in range(20000))
    for name in ("elitea-mcp-tray.log", "elitea-mcp-tray.log.1", "elitea-mcp-serve.log", "elitea-mcp-tray-servers.log",
                 "elitea-mcp-update.log"):
        with open(log_dir / name, "w", encoding="utf-8") as stream:
            while stream.tell() < 20 * 1024 * 1024:
                stream.write(block)
    (log_dir / "elitea-mcp-tray.out").write_bytes(os.urandom(3 * 1024 * 1024))
    for index in range(100):
        (log_dir / f"server{index}-error.txt").write_text("x" * 60 * 1024)

    path = export(config_file, log_dir)
    members = read_members(path)

    assert len(members["logs/elitea-mcp-tray.log"]) + len(members.get("logs/elitea-mcp-tray.log.1", b"")) <= MAIN_LOG_BUDGET
    assert len(members["logs/elitea-mcp-serve.log"]) <= MAIN_LOG_BUDGET
    assert len(members["logs/elitea-mcp-tray-servers.log"]) <= SERVER_LOG_BUDGET
    assert len(members["logs/elitea-mcp-tray.out"]) <= DAEMON_OUTPUT_BUDGET
    assert len(members["logs/elitea-mcp-update.log"]) <= UPDATE_LOG_BUDGET
    assert sum(len(data) for name, data in members.items() if name.endswith("-error.txt")) <= REPORTS_BUDGET
    budgets = 2 * (MAIN_LOG_BUDGET + SERVER_LOG_BUDGET + DAEMON_OUTPUT_BUDGET) + UPDATE_LOG_BUDGET + REPORTS_BUDGET
    assert sum(len(data) for name, data in members.items() if name.startswith("logs/")) <= budgets + 64 * 1024
    assert len(members["logs/elitea-mcp-tray.log"]) > MAIN_LOG_BUDGET // 2
    assert members["logs/elitea-mcp-tray.log"].startswith(b"2026-11-16")
    assert path.stat().st_size <= 4 * 1024 * 1024


def test_a_member_lengthened_by_masking_is_cut_back_to_its_budget(folders, leaky_doctor):
    log_dir, config_file = folders
    (log_dir / "elitea-mcp-tray.out").write_text("token=abcd\n" * (DAEMON_OUTPUT_BUDGET // 11 + 100))

    members = read_members(export(config_file, log_dir))

    assert len(members["logs/elitea-mcp-tray.out"]) <= DAEMON_OUTPUT_BUDGET
    assert members["logs/elitea-mcp-tray.out"].startswith(b"token=****\n")


def test_a_small_log_is_read_whole(folders, leaky_doctor):
    log_dir, config_file = folders
    (log_dir / "elitea-mcp-tray.log").write_text("first line\nsecond line\n")

    assert read_members(export(config_file, log_dir))["logs/elitea-mcp-tray.log"] == b"first line\nsecond line\n"


def test_the_tray_log_in_another_folder_is_collected_too(tmp_path, folders, leaky_doctor):
    log_dir, config_file = folders
    own_log = tmp_path / "custom" / "my-tray.log"
    own_log.parent.mkdir()
    own_log.write_text(f"{SURVIVOR_LINE}\n")

    path = build_support_bundle(doctor_context(config_file, log_dir), log_dir, log_files_in(log_dir, own_log))
    members = read_members(path)

    assert SURVIVOR_LINE.encode() in members["logs/other/my-tray.log"]


# Sections and output

def test_a_crashing_doctor_leaves_the_other_sections(folders):
    log_dir, config_file = folders
    config_file.write_text(json.dumps({"auth_token": TOKEN}), encoding="utf-8")
    with patch.object(support_bundle, "run_checks", side_effect=RuntimeError(TOKEN)):
        members = read_members(export(config_file, log_dir))

    assert members["doctor.error.txt"] == b"could not collect: RuntimeError\n"
    assert json.loads(members["config.masked.json"])["auth_token"] == "****"
    assert "versions.txt" in members


def test_no_log_folder_is_said_and_the_rest_still_comes(folders, leaky_doctor):
    log_dir, config_file = folders

    path = build_support_bundle(doctor_context(config_file, log_dir), log_dir, LogFiles.from_default_paths(None))
    members = read_members(path)

    assert members["logs/README.txt"] == b"log folder unavailable: it could not be found or created\n"
    assert {"doctor.txt", "config.error.txt", "versions.txt"} <= set(members)


def test_the_home_folder_is_never_named(folders, leaky_doctor):
    log_dir, config_file = folders
    home = str(Path.home())
    (log_dir / "elitea-mcp-tray.log").write_text(f"open {home}/project and {json.dumps(home)[1:-1]}/x\n")

    members = read_members(export(config_file, log_dir))

    assert not any(home.encode() in data for data in members.values())
    assert members["logs/elitea-mcp-tray.log"] == b"open ~/project and ~/x\n"


def test_a_failed_write_never_leaves_a_zip_under_the_final_name(folders, leaky_doctor):
    log_dir, config_file = folders
    target = log_dir / "bundle.zip"
    original = support_bundle.write_file_atomically

    def write(path, content):
        assert not target.exists()
        original(path, content)
        raise OSError("disk full")
    with patch.object(support_bundle, "write_file_atomically", side_effect=write), pytest.raises(OSError):
        export(config_file, log_dir, target)

    with pytest.raises(OSError):
        with patch("os.replace", side_effect=OSError("disk full")):
            export(config_file, log_dir, target)
    assert sorted(path.name for path in log_dir.iterdir()) == ["bundle.zip"]


@pytest.mark.skipif(os.name == "nt", reason="owner-only modes are POSIX")
def test_the_bundle_is_readable_by_its_owner_only(folders, leaky_doctor):
    log_dir, config_file = folders

    path = export(config_file, log_dir)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert re.fullmatch(r"elitea-mcp-support-\d{8}-\d{6}\.zip", path.name)


def test_output_paths(tmp_path):
    (tmp_path / "dir").mkdir()

    assert resolve_output(tmp_path / "dir").parent == tmp_path / "dir"
    assert resolve_output(f"{tmp_path / 'new'}{os.sep}").parent == tmp_path / "new"
    assert resolve_output(tmp_path / "given.zip") == tmp_path / "given.zip"
    assert resolve_output(tmp_path / "given") == tmp_path / "given.zip"
    with pytest.raises(FileNotFoundError):
        resolve_output(tmp_path / "missing" / "b.zip")


def test_the_bundle_leaves_the_log_masking_secrets_alone(folders, leaky_doctor):
    log_dir, config_file = folders
    write_canary_setup(log_dir, config_file)
    logging_setup.register_log_secrets({"auth_token": "already-registered-7082"})
    before = logging_setup.get_log_secrets()

    export(config_file, log_dir)

    assert logging_setup.get_log_secrets() is before


# Tray

@pytest.fixture
def app(folders):
    log_dir, config_file = folders
    controller = MagicMock(daemon_mode=False, is_running=False)
    with patch.object(tray_module, "get_server_controller", return_value=controller), \
            patch.object(tray_module, "UpdateChecker"), patch.object(tray_module.MCPTrayApp, "_read_config", return_value=None):
        app = tray_module.MCPTrayApp()
    app.platform.open_file = MagicMock()
    app.show_notification = MagicMock()
    app.show_error = MagicMock()
    paths = {"tray_log": log_dir / "elitea-mcp-tray.log", "serve_log": log_dir / "elitea-mcp-serve.log",
             "update_log": log_dir / "elitea-mcp-update.log"}
    with patch.object(tray_module, "get_default_paths", return_value=paths), \
            patch.object(tray_module, "get_config_file", return_value=config_file):
        yield app


def wait_for_idle(app):
    finished = threading.Event()
    app.update_menu = MagicMock(side_effect=lambda: finished.set() if app.help_action is None else None)
    return finished


def find_help_items(app):
    return next(item for item in app.get_menu_items() if item.text == "Help").submenu.items


def test_export_from_the_tray_saves_the_bundle_and_opens_its_folder(app, folders, leaky_doctor):
    log_dir, config_file = folders
    write_canary_setup(log_dir, config_file)
    logging_setup.register_log_secrets({"auth_token": canary(46, False)})
    (log_dir / "elitea-mcp-tray.log").write_text(f"rotated {canary(46, False)}\n")
    finished = wait_for_idle(app)

    item = find_help_items(app)[1]
    assert item.text == "Export Support Bundle"
    item(MagicMock())
    assert finished.wait(10)

    bundles = list(log_dir.glob("elitea-mcp-support-*.zip"))
    assert len(bundles) == 1
    app.platform.open_file.assert_called_once_with(log_dir)
    app.show_notification.assert_called_with(
        "Support Bundle", f"Support bundle saved: {bundles[0].name}. Review it, then attach it to your bug report.")
    assert leaky_doctor.call_args.args[0].list_local_session == app.list_local_session
    assert find_canary_cores(read_members(bundles[0])) == set()
    app.show_error.assert_not_called()


def test_one_help_action_at_a_time(app, folders):
    release, started = threading.Event(), threading.Event()
    started_count = [0]

    def slow_doctor(context, on_results=None):
        started_count[0] += 1
        started.set()
        release.wait(10)
        return [ok("Token", "accepted")]
    finished = wait_for_idle(app)
    with patch.object(support_bundle, "run_checks", side_effect=slow_doctor), \
            patch.object(tray_module, "run_checks") as diagnostics_run:
        app.export_support_bundle()
        assert started.wait(5)
        assert [(item.text, item.enabled) for item in find_help_items(app)] == [
            ("Run Diagnostics", False), ("Exporting Support Bundle…", False)]
        app.run_diagnostics()
        app.export_support_bundle()
        release.set()
        assert finished.wait(10)

    diagnostics_run.assert_not_called()
    assert started_count == [1]
    assert len(list(folders[0].glob("elitea-mcp-support-*.zip"))) == 1
    assert [(item.text, item.enabled) for item in find_help_items(app)] == [
        ("Run Diagnostics", True), ("Export Support Bundle", True)]


def test_diagnostics_disables_the_export(app):
    app.help_action = tray_module.DIAGNOSTICS

    assert [(item.text, item.enabled) for item in find_help_items(app)] == [
        ("Running Diagnostics…", False), ("Export Support Bundle", False)]


def test_a_failed_export_reports_the_reason(app, leaky_doctor):
    finished = wait_for_idle(app)
    with patch.object(tray_module, "build_support_bundle", side_effect=PermissionError("denied")):
        app.export_support_bundle()
        assert finished.wait(5)

    app.show_error.assert_called_once_with("Support Bundle", "Could not export the support bundle: denied")


def test_the_tray_saves_to_the_home_folder_when_the_logs_folder_cannot_be_made(app, leaky_doctor):
    finished = wait_for_idle(app)
    Path.home().mkdir(parents=True, exist_ok=True)
    with patch.object(tray_module, "get_default_paths", side_effect=PermissionError("read-only")):
        app.export_support_bundle()
        assert finished.wait(5)

    assert len(list(Path.home().glob("elitea-mcp-support-*.zip"))) == 1
    app.show_error.assert_not_called()


# CLI

def run_cli(argv):
    with patch.object(sys, "argv", ["elitea-mcp", *argv]):
        main_module.run_cli()


@pytest.fixture
def cli_paths(folders):
    log_dir, _ = folders
    paths = {"tray_log": log_dir / "elitea-mcp-tray.log", "serve_log": log_dir / "elitea-mcp-serve.log",
             "update_log": log_dir / "elitea-mcp-update.log"}
    with patch.object(main_module, "get_default_paths", return_value=paths) as fake:
        yield fake


def test_the_cli_prints_the_doctor_as_it_runs_and_saves_the_bundle(tmp_path, folders, cli_paths, leaky_doctor, capsys):
    log_dir, config_file = folders
    write_canary_setup(log_dir, config_file)
    output = tmp_path / "out"
    output.mkdir()

    run_cli(["support-bundle", "--output", str(output), "--config", str(config_file)])

    printed = capsys.readouterr().out
    bundles = list(output.glob("elitea-mcp-support-*.zip"))
    assert len(bundles) == 1
    assert printed.startswith("ELITEA MCP doctor\n  ✓ Token: accepted\n")
    assert printed.endswith(f"\nSupport bundle saved: {bundles[0]}. Review it, then attach it to your bug report.\n")
    assert leaky_doctor.call_args.args[0].config_file == config_file.resolve()
    cli_paths.assert_called_with(create=False)


def test_the_cli_defaults_to_the_current_folder(tmp_path, folders, cli_paths, leaky_doctor, monkeypatch):
    _, config_file = folders
    monkeypatch.chdir(tmp_path)

    run_cli(["support-bundle", "--config", str(config_file)])

    assert len(list(tmp_path.glob("elitea-mcp-support-*.zip"))) == 1


def test_the_cli_exits_1_before_running_doctor_when_the_output_folder_is_missing(tmp_path, folders, cli_paths, leaky_doctor, capsys):
    _, config_file = folders
    with pytest.raises(SystemExit) as exited:
        run_cli(["support-bundle", "--config", str(config_file), "--output", str(tmp_path / "missing" / "b.zip")])

    assert exited.value.code == 1
    assert "Could not write the support bundle: No such folder" in capsys.readouterr().err
    leaky_doctor.assert_not_called()


def test_the_cli_writes_utf8_when_redirected_on_a_legacy_code_page(tmp_path, folders, cli_paths, leaky_doctor):
    _, config_file = folders
    redirected = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    with patch.object(sys, "stdout", redirected):
        main_module.support_bundle(str(tmp_path), str(config_file))

    redirected.flush()
    assert "✓ Token: accepted" in redirected.buffer.getvalue().decode("utf-8")


def test_a_progress_line_that_cannot_print_does_not_lose_the_report(tmp_path, folders, cli_paths, leaky_doctor):
    _, config_file = folders
    with patch.object(main_module, "print_doctor_group", side_effect=UnicodeEncodeError("cp1252", "✓", 0, 1, "x")):
        main_module.support_bundle(str(tmp_path), str(config_file))

    assert "doctor.txt" in read_members(next(tmp_path.glob("elitea-mcp-support-*.zip")))


def test_help_lists_support_bundle(capsys):
    with patch.object(sys, "argv", ["elitea-mcp", "--help"]), pytest.raises(SystemExit):
        main_module.run_cli()
    assert "support-bundle" in capsys.readouterr().out
