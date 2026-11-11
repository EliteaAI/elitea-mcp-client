import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlsplit

import requests

from ..config import is_ssl_verification_enabled

REQUEST_TIMEOUT = 10


class PreflightError(Exception):
    """Connection settings the platform cannot work with, described so the user knows what to fix."""


@dataclass(frozen=True)
class PlatformCheck:
    title: str
    detail: str = ""
    error: Optional[PreflightError] = None


@dataclass
class PlatformCheckRun:
    """The result of every platform step, and the base URL, project id and headers they resolved."""
    checks: list = field(default_factory=list)
    origin: Optional[str] = None
    project_id: Optional[int] = None
    reachable: bool = False
    headers: Optional[dict] = None


def validate_platform_config(config: dict, notify: Callable[[str], None] = print) -> dict:
    """Return the config with a base deployment URL and an integer project id, once the platform has accepted the token and project."""
    run = check_platform(config, notify, fail_fast=True)
    failed = next((check for check in run.checks if check.error), None)
    if failed:
        raise failed.error
    return {**config, "deployment_url": run.origin, "project_id": run.project_id}


def check_platform(config: dict, notify: Callable[[str], None] = print, fail_fast=False) -> PlatformCheckRun:
    """Run the platform steps in order, one result each; fail_fast stops at the first failure, so start-up sends no request it does not need."""
    run = PlatformCheckRun()
    verify = is_ssl_verification_enabled(config)
    for title, step in PLATFORM_STEPS:
        try:
            run.checks.append(PlatformCheck(title, step(run, config, verify, notify)))
        except PreflightError as error:
            run.checks.append(PlatformCheck(title, error=error))
            if fail_fast:
                break
    return run


def check_deployment_url(run, config, verify, notify):
    raw_url = config.get("deployment_url")
    run.origin = normalize_deployment_url(raw_url)
    if run.origin != raw_url.strip().rstrip("/"):
        notify(f'Deployment URL "{raw_url}" includes a page or endpoint path; connecting to the base address {run.origin} instead.')
    return run.origin


def check_project_id(run, config, verify, notify):
    run.project_id = validate_project_id(config.get("project_id"))
    return str(run.project_id)


def check_platform_reachable(run, config, verify, notify):
    origin = require_origin(run)
    started = time.monotonic()
    validate_platform_reachable(origin, verify)
    run.reachable = True
    return f"{origin}, {round((time.monotonic() - started) * 1000)} ms"


def check_token(run, config, verify, notify):
    """Send the token only to an address that answered like ELITEA, never to a mistyped or hostile one."""
    origin = require_origin(run)
    token = validate_auth_token(config.get("auth_token"))
    if not run.reachable:
        raise PreflightError("Cannot check the token until the platform is reachable.")
    headers = {"Authorization": f"Bearer {token}"}
    validate_token_accepted(origin, headers, verify)
    run.headers = headers
    return "accepted"


def check_project_access(run, config, verify, notify):
    origin = require_origin(run)
    if run.project_id is None:
        raise PreflightError("Cannot check project access: \"project_id\" is not valid.")
    if run.headers is None:
        raise PreflightError("Cannot check project access until the token check passes.")
    validate_project_access(origin, run.project_id, run.headers, verify)
    return f"project {run.project_id}"


def require_origin(run):
    if run.origin is None:
        raise PreflightError("Cannot check: the deployment URL is not valid.")
    return run.origin


PLATFORM_STEPS = (
    ("Deployment URL", check_deployment_url),
    ("Project ID", check_project_id),
    ("Platform reachable", check_platform_reachable),
    ("Token", check_token),
    ("Project access", check_project_access),
)


def normalize_deployment_url(raw_url) -> str:
    """Reduce the deployment URL to scheme, host and port, which is all the platform connection uses."""
    parts = urlsplit((raw_url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise PreflightError(
            f'Deployment URL "{raw_url}" is not a valid URL. Use the base address of ELITEA, for example https://dev.elitea.ai'
        )
    return f"{parts.scheme}://{parts.netloc}"


def validate_project_id(value) -> int:
    """Return the configured project id as an integer."""
    if value is None or value == "":
        raise PreflightError('"project_id" is not set. Run Bootstrap or add "project_id": <number> to config.')
    try:
        return int(str(value).strip())
    except ValueError:
        raise PreflightError(f'"project_id" must be a number, got "{value}".') from None


def validate_auth_token(token) -> str:
    """Return the configured token, which every platform call needs."""
    if not token:
        raise PreflightError('"auth_token" is not set. Run Bootstrap or add "auth_token" to config.')
    return token


def validate_platform_reachable(origin: str, verify: bool):
    """Prove the address answers like an ELITEA platform before a socket is opened to it."""
    response = _get(f"{origin}/socket.io/?EIO=4&transport=polling", origin, verify)
    if response.status_code != 200 or not response.headers.get("content-type", "").startswith("text/plain"):
        raise PreflightError(
            f"{origin} responded, but it is not an ELITEA platform (HTTP {response.status_code} at /socket.io/). Check the deployment URL."
        )


def validate_token_accepted(origin: str, headers: dict, verify: bool):
    """Prove the platform accepts the token, since the socket connects even with a bad one."""
    response = _get(f"{origin}/api/v2/auth/user/", origin, verify, headers)
    if response.status_code == 401 or response.is_redirect:
        raise PreflightError("Authentication failed: the token is invalid or expired. Generate a new token in ELITEA → Settings → Tokens.")
    if response.status_code != 200:
        raise PreflightError(f"Could not verify the token: {origin} answered HTTP {response.status_code}.")


def validate_project_access(origin: str, project_id: int, headers: dict, verify: bool):
    """Prove the token's user is a member of the project, since the platform ignores servers registered for any other project."""
    response = _get(f"{origin}/api/v2/elitea_core/tags/prompt_lib/{project_id}", origin, verify, headers)
    if response.status_code in (403, 404):
        raise PreflightError(
            f'Your token has no access to project {project_id}, or the project does not exist. Check "project_id" in config.'
        )
    if response.status_code != 200:
        raise PreflightError(f"Could not verify access to project {project_id}: {origin} answered HTTP {response.status_code}.")


def _get(url: str, origin: str, verify: bool, headers: dict = None) -> requests.Response:
    """GET a platform URL, turning network failures into messages that name the cause."""
    try:
        return requests.get(url, headers=headers, verify=verify, timeout=REQUEST_TIMEOUT, allow_redirects=False)
    except requests.exceptions.SSLError:
        raise PreflightError(
            f'SSL certificate verification failed for {origin}. If you are behind a corporate proxy, set "ssl_verify": false or configure CA certificates.'
        ) from None
    except requests.exceptions.Timeout:
        raise PreflightError(f"Cannot reach {origin}: the connection timed out. Check the URL, VPN or proxy.") from None
    except requests.exceptions.ConnectionError as error:
        cause = "DNS lookup failed" if "NameResolutionError" in str(error) else "connection refused or dropped"
        raise PreflightError(f"Cannot reach {origin}: {cause}. Check the URL, VPN or proxy.") from None
