"""The rules of the Settings window, kept free of Tk so they hold and can be tested on a Python without it."""
import ipaddress
import logging
import sys
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from urllib.parse import urlsplit

from .config import is_ssl_verification_enabled, load_config, update_config
from .utils.logging_setup import register_log_secrets
from .utils.preflight import PreflightError, check_platform, normalize_deployment_url

logger = logging.getLogger(__name__)

class SettingsExit(IntEnum):
    """How the Settings window closed, as its process exit code; 10 and up, so a crash (1), a usage error (2) or a Windows abort() (3) never reads as one."""
    CANCELLED = 0
    SAVED = 10
    SAVED_RESTART = 11
    TK_MISSING = 12
    NO_DISPLAY = 13



@dataclass
class ConnectionForm:
    deployment_url: str = ""
    auth_token: str = ""
    project_id: str = ""
    stored_token: str = field(default="", repr=False)
    ssl_verify: object = False


@dataclass(frozen=True)
class CheckLine:
    """One line of a connection test: True passed, False failed, None is a note."""
    passed: object
    title: str
    text: str


def load_form():
    """The form as the window opens it: the saved URL and project, and an empty token field even when a token is saved."""
    config = load_config()
    stored_token = config.get("auth_token")
    form = ConnectionForm(
        deployment_url=str(config.get("deployment_url") or ""),
        project_id=str(config.get("project_id") or ""),
        stored_token=stored_token if isinstance(stored_token, str) else "",
        ssl_verify=config.get("ssl_verify", False),
    )
    register_form_secrets(form)
    return form


def describe_stored_token(form):
    """The saved token as the label beside the empty field shows it, since a Tk entry has no placeholder and a hint inside it would be saved."""
    token = form.stored_token
    if not token:
        return "No token saved"
    return "Saved: ********" + (token[-4:] if len(token) > 4 else "")


def resolve_token(form):
    """The token Test and Save use: the typed one, or the saved one when the field is left empty."""
    return form.auth_token.strip() or form.stored_token


def register_form_secrets(form):
    """Mask every token the window holds in this process's log, the typed one before it is ever sent or saved."""
    for token in (form.auth_token, form.auth_token.strip(), form.stored_token):
        register_log_secrets({"auth_token": token})


def find_field_errors(form):
    """The message for every field that cannot be tested or saved, so the platform checks only ever get values they can send."""
    errors = {}
    url_error = find_url_error(form.deployment_url.strip())
    if url_error:
        errors["deployment_url"] = url_error
    token_error = find_token_error(form)
    if token_error:
        errors["auth_token"] = token_error
    project_error = find_project_error(form.project_id.strip())
    if project_error:
        errors["project_id"] = project_error
    return errors


def find_url_error(url):
    """Why the address cannot be tested or saved, or None."""
    if not url:
        return "Enter the address of ELITEA, for example https://dev.elitea.ai"
    if any(character.isspace() for character in url):
        return "The address cannot contain spaces."
    try:
        normalize_deployment_url(url)
    except (PreflightError, ValueError):
        return "Enter the base address of ELITEA, starting with https:// or http://"
    parts = urlsplit(url)
    if read_port(parts) == 0:
        return "The port in the address must be a number from 1 to 65535."
    if parts.username or parts.password:
        return "Remove the user name and password from the address; the token is the only credential."
    return None


def read_port(parts):
    """The address's port, None when it names none, and 0 when it names one no connection can use."""
    try:
        return parts.port
    except ValueError:
        return 0


def find_token_error(form):
    """Why the typed token cannot be sent; an empty field is fine when a token is saved."""
    typed = form.auth_token.strip()
    if not typed:
        return None if form.stored_token else "Paste a token from ELITEA → Settings → Tokens."
    if not typed.isascii() or not typed.isprintable() or " " in typed:
        return "The token has spaces or hidden characters; copy it again from ELITEA → Settings → Tokens."
    return None


def find_project_error(project_id):
    """Why the project ID cannot be saved, or None."""
    if not project_id:
        return "Enter the project ID, a number shown in the ELITEA address bar."
    if not project_id.isdecimal() or int(project_id) < 1:
        return "The project ID must be a positive number."
    return None


def build_platform_config(form):
    """The config the platform checks run on: the form's values with the saved TLS setting."""
    return {
        "deployment_url": form.deployment_url.strip(),
        "auth_token": resolve_token(form),
        "project_id": form.project_id.strip(),
        "ssl_verify": form.ssl_verify,
    }


def check_connection(form):
    """One line per platform check for the values in the form, never raising, with notes on how the token travels."""
    register_form_secrets(form)
    config = build_platform_config(form)
    notes = []
    try:
        run = check_platform(config, notes.append)
    except Exception as error:
        logger.exception("Connection test stopped by an unexpected error")
        return [CheckLine(False, "Connection test", f"Stopped by an unexpected {type(error).__name__}; see the log.")]
    lines = [CheckLine(check.error is None, check.title, str(check.error) if check.error else check.detail)
             for check in run.checks]
    lines += [CheckLine(None, "Note", note) for note in notes]
    lines += [CheckLine(None, "Note", note) for note in describe_token_transport(config)]
    return lines


def describe_token_transport(config):
    """Notes on a token that would travel unencrypted or to an unverified certificate."""
    parts = urlsplit(config["deployment_url"])
    if parts.scheme == "http" and not is_loopback(parts.hostname):
        yield "The token is sent unencrypted over http."
    if parts.scheme == "https" and not is_ssl_verification_enabled(config):
        yield 'The TLS certificate is not verified ("ssl_verify" is off).'


def is_loopback(hostname):
    """Whether the host is this machine, where plain http never leaves it."""
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname or "").is_loopback
    except ValueError:
        return False


def save_form(form):
    """Write the three connection keys, the URL as the base address the client connects to, keeping every other key; an empty token field keeps the token the file holds now, even one changed since the window opened."""
    register_form_secrets(form)
    deployment_url = normalize_deployment_url(form.deployment_url.strip())
    typed_token = form.auth_token.strip()
    project_id = int(form.project_id.strip())

    def apply(config):
        saved_token = config.get("auth_token")
        auth_token = typed_token or (saved_token if isinstance(saved_token, str) and saved_token else form.stored_token)
        changed = (config.get("deployment_url"), saved_token, config.get("project_id")) != (
            deployment_url, auth_token, project_id)
        config.update({"deployment_url": deployment_url, "auth_token": auth_token, "project_id": project_id})
        return changed

    return update_config(apply, "The connection settings")


def exit_code_for_cli(exit_code):
    """The exit code a terminal or script expects, 0 for a closed or saved window, with the reason printed when none could open; the tray reads the window's own codes instead."""
    if exit_code == SettingsExit.TK_MISSING:
        print(f"The Settings window needs Tk, which this Python lacks. {describe_tk_install()}", file=sys.stderr)
        return 1
    if exit_code == SettingsExit.NO_DISPLAY:
        print("The Settings window needs a graphical display.", file=sys.stderr)
        return 1
    return 0


def describe_tk_install(platform_name=None, prefix=None, os_release=Path("/etc/os-release")):
    """How to add Tk to the Python the client runs on, as precisely as the platform tells; a venv or pipx install is judged by the Python it was made from."""
    platform_name = platform_name or sys.platform
    prefix = prefix or sys.base_prefix
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if platform_name == "darwin" and ("homebrew" in prefix.lower() or "/Cellar/" in prefix):
        return f"Install it with: brew install python-tk@{version}"
    if platform_name == "win32":
        return 'Run the Python installer again, choose "Modify" and select "tcl/tk and IDLE".'
    if platform_name.startswith("linux"):
        family = read_os_family(os_release)
        if family & {"debian", "ubuntu"}:
            return "Install it with: sudo apt install python3-tk"
        if family & {"fedora", "rhel", "centos"}:
            return "Install it with: sudo dnf install python3-tkinter"
    return "Install Tk support for this Python."


def read_os_family(os_release):
    """The distribution and the ones it is like, from /etc/os-release."""
    try:
        text = os_release.read_text(encoding="utf-8")
    except OSError:
        return set()
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    return {word.strip('"').lower() for key in ("ID", "ID_LIKE") for word in values.get(key, "").split()}

