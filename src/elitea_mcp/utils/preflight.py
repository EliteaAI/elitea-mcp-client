from typing import Callable
from urllib.parse import urlsplit

import requests

from ..config import is_ssl_verification_enabled

REQUEST_TIMEOUT = 10


class PreflightError(Exception):
    """Connection settings the platform cannot work with, described so the user knows what to fix."""


def validate_platform_config(config: dict, notify: Callable[[str], None] = print) -> dict:
    """Return the config with a base deployment URL and an integer project id, once the platform has accepted the token and project."""
    raw_url = config.get("deployment_url")
    origin = normalize_deployment_url(raw_url)
    if origin != raw_url.strip().rstrip("/"):
        notify(f'Deployment URL "{raw_url}" includes a page or endpoint path; connecting to the base address {origin} instead.')
    project_id = validate_project_id(config.get("project_id"))
    verify = is_ssl_verification_enabled(config)
    validate_platform_reachable(origin, verify)
    headers = {"Authorization": f"Bearer {validate_auth_token(config.get('auth_token'))}"}
    validate_token_accepted(origin, headers, verify)
    validate_project_access(origin, project_id, headers, verify)
    return {**config, "deployment_url": origin, "project_id": project_id}


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
