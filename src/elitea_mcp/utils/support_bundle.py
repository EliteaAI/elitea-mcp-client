"""`elitea-mcp support-bundle` and tray Help → Export Support Bundle: one zip for a bug report, every byte masked on its way in."""
import importlib.metadata
import io
import json
import logging
import os
import platform
import re
import shlex
import sys
import urllib.parse
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from ..config import write_file_atomically
from ..config_errors import backup_file_for, list_hidden_values, parse_config
from .diagnostics import REPORT_FILE_NAME, describe_os, format_report, read_config, run_checks
from .logging_setup import build_daemon_output_path, build_server_output_log_path
from .package_updates import get_installed_version
from .self_update import detect_install_method
from .server_loader import (MASK, MIN_SECRET_LENGTH, collect_config_secrets, is_credential_name, is_plain_setting,
                            list_secret_forms, mask_secrets, mask_url_secrets, redact_urls)

SAVED_MESSAGE = "Support bundle saved: {}. Review it, then attach it to your bug report."
BUNDLE_NAME_FORMAT = "elitea-mcp-support-%Y%m%d-%H%M%S.zip"
MAIN_LOG_BUDGET = 3 * 1024 * 1024
SERVER_LOG_BUDGET = 512 * 1024
DAEMON_OUTPUT_BUDGET = 256 * 1024
UPDATE_LOG_BUDGET = 256 * 1024
REPORTS_BUDGET = 512 * 1024
MAIN_LOG_BACKUPS = 5
SERVER_LOG_BACKUPS = 3
LONG_SECRET_LENGTH = 8
WRAP_INDENT = " " * 16
REPORTED_PACKAGES = ("mcp", "python-socketio", "pystray", "pydantic", "httpx")

LAUNCHERS = frozenset({"npx", "uvx", "pipx", "bunx", "uv", "docker", "podman", "node", "python", "python3", "deno"})
LAUNCHER_SUBCOMMANDS = {"docker": {"run"}, "podman": {"run"}, "uv": {"run", "tool"}, "pipx": {"run"}, "deno": {"run"}}
PACKAGE_FLAGS = {"python": {"-m"}, "python3": {"-m"}, "uvx": {"--from"}, "uv": {"--from"}}
CONTAINER_LAUNCHERS = frozenset({"docker", "podman"})
CONTAINER_ENV_FLAGS = frozenset({"-e", "--env"})
CONTAINER_VALUE_FLAGS = frozenset({"-e", "--env", "-v", "--volume", "--name", "-p", "--publish", "--network", "--mount",
                                   "-w", "--workdir", "-u", "--user", "--env-file", "--entrypoint", "--platform", "-l",
                                   "--label", "--add-host", "--cpus", "-m", "--memory"})
UV_VALUE_FLAGS = frozenset({"--directory", "--project", "--with", "--python", "-p", "--index-url", "--extra-index-url",
                            "--index", "--default-index"})
VALUE_FLAGS = {"docker": CONTAINER_VALUE_FLAGS, "podman": CONTAINER_VALUE_FLAGS, "uv": UV_VALUE_FLAGS, "uvx": UV_VALUE_FLAGS,
               "npx": frozenset({"--registry", "--cache", "--userconfig"})}
WINDOWS_SHELL_FLAGS = frozenset({"/c", "/k", "/s", "/q"})
LAUNCHER_SUFFIXES = (".exe", ".cmd", ".bat")
PACKAGE_SHAPE = re.compile(r"(?:[a-z0-9][a-z0-9.-]*(?::\d{1,5})?/)?(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*"
                           r"(?:/[a-z0-9][a-z0-9._~-]*)*(?:@[\w.^~<>=*-]+|:[\w.-]+)?")
HEX_RUN = re.compile(r"[0-9a-f]{16}")
COMBINED_SHORT_FLAGS = re.compile(r"[a-z]{1,3}")
PLAIN_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
WEB_URL = re.compile(r"(?:git\+)?https?://[^\s/?#]")
ANY_SCHEME_URL = re.compile(r"[a-zA-Z][\w+.-]*://\S+")
GENERATED_PATH_SEGMENT = re.compile(r"(?=[^/]*\d)[^/]{8,}")
NAMED_VALUE = re.compile(r"\s{0,8}([\w.-]{1,64})\s{0,8}[:=]\s{0,8}(.+)", re.DOTALL)
CREDENTIAL_SCHEMES = ("bearer", "basic", "token")

KEY_PARTS = (r"(?:token|secret|passw(?:or)?d|pwd|api[_-]?key|apikey|credential|private[_-]?key|access[_-]?key|cookie"
             r"|session[_-]?(?:id|key)|connection[_-]?string|(?:ssh|signing)[_-]?key)")
CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)(authorization[\"']?\s{0,4}[:=]\s{0,4}[\"']?(?:(?:bearer|basic|token|digest)\s{1,4})?)[^\s\"',;}\]]+"),
    re.compile(r"(?i)(?<![\w-])((?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(?<![A-Za-z0-9])([\w-]{0,16}" + KEY_PARTS + r"[\w-]{0,16}[\"']?\s{0,4}[:=]\s{0,4}[\"']?)"
               r"(?!\d+(?![^\s\"',;&}\]]))[^\s\"',;&}\]]{4,}"),
    re.compile(r"(?<![\w-])eyJ[\w-]{2,}\.[\w-]{2,}\.[\w-]*"),
    re.compile(r"(?<![\w-])(?:sk-|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|xox[abpors]-|ntn_|AIza|ATATT3|hf_|npm_|pypi-"
               r"|dckr_pat_)[\w-]{8,}|(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}"),
)
PRIVATE_KEY_BEGIN = "-----BEGIN "
PRIVATE_KEY_END = "-----END "
PRIVATE_KEY_LABEL_END = "PRIVATE KEY-----"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BundleEntry:
    name: str
    text: str
    budget: Optional[int] = None
    wrapped: bool = False

    @property
    def is_log(self):
        return self.name.startswith("logs/")


@dataclass(frozen=True)
class LogFiles:
    """Where the logs are: the log folder, the tray and serve logs, the update log, and the tray's own log when it is elsewhere."""
    folder: Optional[Path]
    tray_log: Optional[Path] = None
    serve_log: Optional[Path] = None
    update_log: Optional[Path] = None
    own_log: Optional[Path] = None

    @classmethod
    def from_default_paths(cls, paths, own_log=None):
        """None for paths when the log folder cannot be named or created: the bundle then says so instead of listing logs."""
        if paths is None:
            return cls(None, own_log=own_log and Path(own_log))
        tray_log = Path(paths["tray_log"])
        own_log = Path(own_log) if own_log and Path(own_log) != tray_log else None
        serve_log, update_log = paths.get("serve_log"), paths.get("update_log")
        return cls(tray_log.parent, tray_log, serve_log and Path(serve_log), update_log and Path(update_log), own_log)


@dataclass(frozen=True)
class BundleSources:
    context: object
    log_files: LogFiles
    on_doctor_results: Optional[Callable[[list], None]] = None


def build_support_bundle(context, output, log_files, live_secrets=(), on_doctor_results=None):
    """Collect every section, mask each file the same way and save the zip in one step; returns its path."""
    path = resolve_output(output)
    entries = collect_entries(BundleSources(context, log_files, on_doctor_results))
    secrets = collect_bundle_secrets(context, live_secrets)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for entry in entries:
            bundle.writestr(entry.name, mask_entry(entry, secrets).encode("utf-8"))
    write_file_atomically(path, buffer.getvalue())
    return path


def resolve_output(output, now=None):
    """A folder gets the default name, made first when given with a trailing separator; a file is used as given, with .zip added."""
    name = (now or datetime.now()).strftime(BUNDLE_NAME_FORMAT)
    text = os.fspath(output)
    path = Path(text).expanduser()
    if text.endswith(("/", os.sep)):
        path.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        return path / name
    path = path if path.suffix.lower() == ".zip" else path.with_name(path.name + ".zip")
    if not path.parent.is_dir():
        raise FileNotFoundError(f"No such folder: {path.parent}")
    return path


# Sections

def collect_doctor(sources):
    results = run_checks(sources.context, sources.on_doctor_results and report_quietly(sources.on_doctor_results))
    return [BundleEntry("doctor.txt", format_report(results))]


def report_quietly(callback):
    """Printing progress must never cost the report itself, such as a console that cannot encode a mark."""
    def report(group):
        try:
            callback(group)
        except Exception:
            logger.debug("Could not show doctor progress", exc_info=True)
    return report


def collect_config(sources):
    context = sources.context
    if context.raw is None and context.config_error is None and context.config_read_error is None:
        read_config(context)
    if context.raw is None:
        return [BundleEntry("config.error.txt", describe_config_problem(context) + "\n")]
    return [BundleEntry("config.masked.json", json.dumps(project_config(context.raw, []), indent=2, ensure_ascii=False) + "\n")]


def describe_config_problem(context):
    if context.config_error is not None:
        return context.config_error.summary
    if context.config_read_error is not None:
        return f"cannot read {context.config_file}: {context.config_read_error.strerror or context.config_read_error}"
    return f"no config file at {context.config_file}"


def collect_logs(sources):
    log_files = sources.log_files
    if log_files.folder is None:
        return [BundleEntry("logs/README.txt", "log folder unavailable: it could not be found or created\n")]
    entries, summary = [], []
    for paths, budget, wrapped in list_log_families(log_files):
        left = budget
        for path in paths:
            if left <= 0 or not path.is_file():
                continue
            try:
                text, size = read_tail(path, left)
            except OSError as error:
                summary.append(f"{path}: skipped ({type(error).__name__})")
                continue
            kept = min(len(text.encode("utf-8")), left)
            left -= kept
            entries.append(BundleEntry(name_log_member(log_files, path), text, kept, wrapped))
            summary.append(f"{path}: kept {kept} of {size} bytes")
    folders = sorted({str(log_files.folder), *([str(log_files.own_log.parent)] if log_files.own_log else [])})
    header = [f"Logs were looked for in: {', '.join(folders)}"]
    if not entries:
        header.append(f"no log files found in {', '.join(folders)}")
    return entries + [BundleEntry("logs/README.txt", "\n".join(header + summary) + "\n")]


def list_log_families(log_files):
    """Each family of files with the bytes they share, newest first: a main log and its rotated copies, its server output, its daemon output, the update log, and the reports."""
    families = []
    for logs in ([log_files.own_log, log_files.tray_log], [log_files.serve_log]):
        logs = [log for log in logs if log is not None]
        families.append(([path for log in logs for path in with_backups(log, MAIN_LOG_BACKUPS)], MAIN_LOG_BUDGET, False))
        families.append(([path for log in logs for path in with_backups(build_server_output_log_path(log), SERVER_LOG_BACKUPS)],
                         SERVER_LOG_BUDGET, True))
        families.append(([build_daemon_output_path(log) for log in logs], DAEMON_OUTPUT_BUDGET, False))
    if log_files.update_log is not None:
        families.append(([log_files.update_log], UPDATE_LOG_BUDGET, False))
    families.append((list_reports(log_files), REPORTS_BUDGET, False))
    return families


def with_backups(path, count):
    return [path] + [path.with_name(f"{path.name}.{index}") for index in range(1, count + 1)]


def list_reports(log_files):
    folders = {log_files.folder, *([log_files.own_log.parent] if log_files.own_log else [])}
    reports = [path for folder in folders for path in [*folder.glob("*-error.txt"), folder / REPORT_FILE_NAME]]
    return sorted((path for path in reports if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)


def read_tail(path, budget):
    """The newest bytes of a file, starting at a line when it had to be cut, decoded leniently because a daemon's output may hold binary."""
    with open(path, "rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        start = max(0, size - budget)
        stream.seek(start)
        data = stream.read(budget)
    if start:
        data = data[data.find(b"\n") + 1:] if b"\n" in data else b""
    return data.decode("utf-8", errors="replace"), size


def name_log_member(log_files, path):
    return f"logs/{path.name}" if path.parent == log_files.folder else f"logs/other/{path.name}"


def collect_versions(sources):
    method, _ = detect_install_method()
    lines = [f"elitea-mcp {get_installed_version() or 'unknown (source checkout)'}", f"installed via {method}",
             f"Python {platform.python_version()} ({sys.executable})", f"{describe_os()} {platform.machine()}",
             *(describe_package(name) for name in REPORTED_PACKAGES),
             f"logs: {sources.log_files.folder or 'unavailable'}", f"config: {sources.context.config_file.parent}"]
    return [BundleEntry("versions.txt", "\n".join(lines) + "\n")]


def describe_package(name):
    try:
        return f"{name} {importlib.metadata.version(name)}"
    except importlib.metadata.PackageNotFoundError:
        return f"{name} not installed"


SECTIONS = (("doctor", collect_doctor), ("config", collect_config), ("logs", collect_logs), ("versions", collect_versions))


def collect_entries(sources):
    """Doctor runs first: it reads the config the config section reuses and writes the server reports the logs section picks up."""
    entries = []
    for name, collect in SECTIONS:
        try:
            entries.extend(collect(sources))
        except Exception as error:
            logger.debug(f"Support bundle section {name} failed", exc_info=True)
            entries.append(BundleEntry(f"{name}.error.txt", f"could not collect: {type(error).__name__}\n"))
    return entries


# The config, rebuilt from the keys this client reads

def project_config(raw, hidden):
    """Each known key by its rule; any other key keeps its name and loses its value, so a setting added later is shown only on purpose."""
    return project_object(raw, CONFIG_RULES, hidden)


def project_object(raw, rules, hidden):
    return {key: rules.get(key, hide_unknown)(key, value, hidden, raw) for key, value in raw.items()}


def keep(key, value, hidden, owner):
    return value if is_scalar(value) else hide_all(value, hidden, is_credential_name(key))


def keep_host(key, value, hidden, owner):
    if not isinstance(value, str) or not WEB_URL.match(value):
        return hide_all(value, hidden, True)
    hidden.extend(collect_url_secrets(value))
    return redact_urls(mask_url_secrets(value))


def hide_token(key, value, hidden, owner):
    return None if value in (None, "") else hide_all(value, hidden, True)


def hide_header_values(key, value, hidden, owner):
    if not isinstance(value, dict):
        return hide_all(value, hidden, True)
    return {name: hide_all(setting, hidden, True) for name, setting in value.items()}


def hide_env_values(key, value, hidden, owner):
    if not isinstance(value, dict):
        return hide_all(value, hidden, True)
    return {name: setting if is_plain_setting(name, setting) else hide_all(setting, hidden, is_credential_name(name))
            for name, setting in value.items()}


def project_servers(key, value, hidden, owner):
    if not isinstance(value, dict):
        return hide_all(value, hidden, False)
    return {name: project_object(server, SERVER_RULES, hidden) if isinstance(server, dict) else hide_command_line(server, hidden)
            for name, server in value.items()}


def project_args(key, value, hidden, owner):
    if isinstance(value, str):
        mask_args(split_args(value), owner.get("command"), owner.get("cwd"), hidden)
        return hide_all(value, hidden, False)
    if not isinstance(value, list) or not all(isinstance(arg, (str, int, float, bool)) for arg in value):
        return hide_all(value, hidden, False)
    return mask_args(value, owner.get("command"), owner.get("cwd"), hidden)


def hide_command_line(value, hidden):
    """A server written as one command line: masked whole, with each of its values remembered as its args would be."""
    if isinstance(value, str) and (words := split_args(value)):
        mask_args(words[1:], words[0], None, hidden)
    return hide_all(value, hidden, False)


def split_args(text):
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def hide_unknown(key, value, hidden, owner):
    return hide_all(value, hidden, is_credential_name(key))


CONFIG_RULES = {
    "$schema": keep, "deployment_url": keep_host, "auth_token": hide_token, "project_id": keep, "host": keep, "port": keep,
    "timeout": keep, "startup_timeout": keep, "ssl_verify": keep, "update_check": keep,
    "update_check_interval_hours": keep, "log_level": keep, "servers": project_servers, "mcpServers": project_servers,
}
SERVER_RULES = {
    "type": keep, "command": keep, "args": project_args, "env": hide_env_values, "cwd": keep, "encoding": keep,
    "encoding_error_handler": keep, "url": keep_host, "headers": hide_header_values, "stateful": keep,
    "startup_timeout": keep,
}


def is_scalar(value):
    return value is None or isinstance(value, (str, int, float, bool))


def hide_all(value, hidden, credential):
    """Mask a value of any shape and remember its strings, so a log that echoes one is masked too."""
    for leaf in iterate_leaves(value):
        if isinstance(leaf, str):
            remember(leaf, hidden, credential)
    return MASK


def remember(value, hidden, credential):
    """A short masked value is remembered only when its name says credential, so that "stdio" or "run" stay readable in the logs."""
    if len(value) >= LONG_SECRET_LENGTH or (credential and len(value) >= MIN_SECRET_LENGTH):
        hidden.append(value)
    hidden.extend(list_secret_parts(value))


def iterate_leaves(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from iterate_leaves(child)
    elif isinstance(value, list):
        for child in value:
            yield from iterate_leaves(child)
    else:
        yield value


# args: keep what tells a wrong package or path apart, mask the rest

def mask_args(args, command, cwd, hidden):
    launcher = name_launcher(command)
    if launcher == "cmd":
        return mask_shell_args(args, cwd, hidden)
    return mask_launcher_args(args, launcher, cwd, hidden)


def name_launcher(command):
    name = Path(str(command or "").replace("\\", "/")).name.lower()
    for suffix in LAUNCHER_SUFFIXES:
        name = name.removesuffix(suffix)
    return name


def mask_shell_args(args, cwd, hidden):
    """`cmd /c npx …`, the Windows form installers write: keep the switches, then read the command after them as the launcher."""
    kept = []
    for index, arg in enumerate(args):
        if isinstance(arg, str) and arg.lower() in WINDOWS_SHELL_FLAGS:
            kept.append(arg)
            continue
        if isinstance(arg, str) and name_launcher(arg) in LAUNCHERS:
            return kept + [arg] + mask_launcher_args(args[index + 1:], name_launcher(arg), cwd, hidden)
        return kept + mask_launcher_args(args[index:], "", cwd, hidden)
    return kept


def mask_launcher_args(args, launcher, cwd, hidden):
    masked = []
    pending_flag = None
    package_kept = launcher not in LAUNCHERS
    for arg in args:
        if not isinstance(arg, str):
            masked.append(arg)
            pending_flag = None
            continue
        if is_flag(arg):
            masked.append(mask_flag(arg, cwd, hidden))
            pending_flag = None if "=" in arg else arg
            continue
        flag, pending_flag = pending_flag, None
        if flag is not None and is_credential_name(flag):
            masked.append(hide_all(arg, hidden, True))
        elif flag in CONTAINER_ENV_FLAGS and launcher in CONTAINER_LAUNCHERS:
            masked.append(mask_container_env(arg, hidden))
        elif flag in PACKAGE_FLAGS.get(launcher, ()) and is_package(arg):
            masked.append(arg)
            package_kept = True
        elif flag in VALUE_FLAGS.get(launcher, ()):
            masked.append(mask_value(arg, cwd, hidden))
        elif not package_kept and arg in LAUNCHER_SUBCOMMANDS.get(launcher, ()):
            masked.append(arg)
        elif not package_kept and is_package(arg):
            masked.append(arg)
            package_kept = True
        else:
            masked.append(mask_value(arg, cwd, hidden))
            package_kept = package_kept or flag not in PACKAGE_FLAGS.get(launcher, ())
    return masked


def is_flag(arg):
    return len(arg) > 1 and arg.startswith("-") and not PLAIN_NUMBER.fullmatch(arg)


def mask_flag(arg, cwd, hidden):
    """A flag keeps its name; a value written into it stays only when it is harmless and the name does not say credential."""
    if arg.startswith("--"):
        name, separator, value = arg.partition("=")
    elif len(arg) > 2 and "=" not in arg:
        name, separator, value = arg[:2], "", arg[2:]
        if COMBINED_SHORT_FLAGS.fullmatch(value) or PLAIN_NUMBER.fullmatch(value):
            return arg
    else:
        name, separator, value = arg.partition("=")
    if not value:
        return arg
    if is_credential_name(name):
        return f"{name}{separator}{hide_all(value, hidden, True)}"
    return f"{name}{separator}{mask_value(value, cwd, hidden)}"


def mask_container_env(arg, hidden):
    """`-e NAME` passes a value from the environment and names no secret; `-e NAME=VALUE` keeps the name only."""
    name, separator, value = arg.partition("=")
    return f"{name}={hide_all(value, hidden, True)}" if separator else arg


def mask_value(value, cwd, hidden):
    if is_plain_setting("", value):
        return value
    if WEB_URL.match(value):
        hidden.extend(collect_url_secrets(value))
        return redact_urls(mask_url_secrets(value))
    if is_existing_path(value, cwd):
        return value
    return hide_all(value, hidden, False)


def is_package(arg):
    return PACKAGE_SHAPE.fullmatch(arg) is not None and HEX_RUN.search(arg) is None


def is_existing_path(value, cwd):
    if not any(mark in value for mark in ("/", "\\")) and not value.startswith(("~", ".")):
        return False
    path = Path(value).expanduser()
    if not path.is_absolute() and isinstance(cwd, str):
        path = Path(cwd).expanduser() / path
    try:
        return path.exists()
    except (OSError, ValueError):
        return False


# Secrets

def collect_bundle_secrets(context, live_secrets=()):
    """Every value to mask in every file: the config's and its backup's secrets, the values the projection masked, the parts inside composite ones, and what the running tray already masks."""
    values = set(live_secrets)
    hidden = []
    for raw in (context.raw, read_backup(context.config_file)):
        if isinstance(raw, dict):
            values.update(collect_config_secrets(raw))
            project_config(raw, hidden)
    if context.raw is None:
        for owner, value in list_hidden_values(read_text(context.config_file)):
            remember(value, hidden, is_credential_name(owner))
    for value in [*values, *hidden]:
        values.update(list_secret_parts(value))
    values.update(hidden)
    forms = {form for value in values for form in list_secret_forms(value)}
    return tuple(sorted((form for form in forms if len(form) >= MIN_SECRET_LENGTH and form not in "https://"),
                        key=len, reverse=True))


def read_backup(config_file):
    try:
        path = backup_file_for(config_file)
        return parse_config(path.read_text(encoding="utf-8"), path)
    except Exception:
        return None


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def list_secret_parts(value):
    """The credential inside a composite value: after Bearer, after a credential-named key, inside JSON, and the parts of a URL the bundle drops."""
    if not isinstance(value, str):
        return []
    parts = []
    scheme, _, credential = value.strip().partition(" ")
    if credential and scheme.lower() in CREDENTIAL_SCHEMES:
        parts.append(credential.strip())
    named = NAMED_VALUE.fullmatch(value)
    if named and is_credential_name(named.group(1)):
        parts.append(named.group(2).strip())
        parts.extend(list_secret_parts(named.group(2).strip()))
    parts.extend(list_json_secrets(value))
    parts.extend(collect_url_secrets(value))
    return [part for part in parts if len(part) >= MIN_SECRET_LENGTH]


def list_json_secrets(value):
    if not value.lstrip().startswith(("{", "[")):
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    return list(iterate_credential_leaves(parsed, False))


def iterate_credential_leaves(value, credential):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from iterate_credential_leaves(child, credential or is_credential_name(key))
    elif isinstance(value, list):
        for child in value:
            yield from iterate_credential_leaves(child, credential)
    elif isinstance(value, str) and credential:
        yield value
        yield from list_secret_parts(value)


def collect_url_secrets(text):
    """What URL redaction drops from a URL of any scheme: its password, query values and path segments that look generated."""
    found = []
    for match in ANY_SCHEME_URL.finditer(text):
        try:
            parts = urllib.parse.urlsplit(match.group(0))
            password = parts.password
        except ValueError:
            continue
        values = [password, *(value for _, value in urllib.parse.parse_qsl(f"{parts.query}&{parts.fragment}"))]
        values += [segment for segment in parts.path.split("/") if GENERATED_PATH_SEGMENT.fullmatch(segment)]
        found.extend(value for value in values if value and len(value) >= MIN_SECRET_LENGTH)
    return found


# Masking, the same for every file

def mask_entry(entry, secrets):
    text = mask_bundle_text(entry.text, secrets, entry.is_log, entry.wrapped)
    return cut_to_budget(text, entry.budget) if entry.budget is not None else text


def mask_bundle_text(text, secrets, is_log=False, wrapped=False):
    """URLs to their host first, so a secret that overlaps a scheme cannot hide a URL from it, then values, URLs again, credential patterns in logs, and the home folder."""
    lines = text.split("\n")
    if wrapped:
        lines = rejoin_wrapped_lines(lines)
    lines = [redact_urls(line) for line in mask_secrets([redact_urls(line) for line in lines], secrets)]
    text = "\n".join(lines)
    if is_log:
        text = mask_credential_patterns(text)
    return replace_home_folder(text)


def rejoin_wrapped_lines(lines):
    """Server output wrapped at 80 columns continues on a record whose message starts with a run of spaces: put it back on one line so a split secret is whole again."""
    joined = []
    for line in lines:
        marker = line.find("] ")
        if joined and marker != -1 and line[marker + 2:].startswith(WRAP_INDENT):
            joined[-1] = joined[-1].rstrip() + line[marker + 2:].strip()
        else:
            joined.append(line)
    return joined


def mask_credential_patterns(text):
    """Credentials the config does not know, such as a rotated token in an old log, by how they are written rather than by value."""
    text = mask_private_keys(text)
    lines = []
    for line in text.split("\n"):
        for pattern in CREDENTIAL_PATTERNS:
            line = pattern.sub(lambda match: (match.group(1) if match.re.groups else "") + MASK, line)
        lines.append(line)
    return "\n".join(lines)


def mask_private_keys(text):
    position = 0
    while (begin := text.find(PRIVATE_KEY_BEGIN, position)) != -1:
        label_end = text.find("-----", begin + len(PRIVATE_KEY_BEGIN))
        if label_end == -1:
            break
        body_start = label_end + len("-----")
        if not text[begin:body_start].endswith(PRIVATE_KEY_LABEL_END):
            position = body_start
            continue
        end = text.find(PRIVATE_KEY_END, body_start)
        end = len(text) if end == -1 else end
        text = f"{text[:body_start]}\n{MASK}\n{text[end:]}"
        position = body_start + len(MASK) + 2
    return text


def replace_home_folder(text):
    home = str(Path.home())
    if len(home) <= 1:
        return text
    for form in sorted({home, home.replace("\\", "\\\\"), home.replace("\\", "/")}, key=len, reverse=True):
        text = text.replace(form, "~")
    return text


def cut_to_budget(text, budget):
    """Masking may lengthen a log, so it is cut again to what was read, from the start of a line."""
    data = text.encode("utf-8")
    if len(data) <= budget:
        return text
    data = data[len(data) - budget:]
    return data[data.find(b"\n") + 1:].decode("utf-8", errors="ignore") if b"\n" in data else ""
