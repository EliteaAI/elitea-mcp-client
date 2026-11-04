import importlib.metadata
import logging
import platform
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import requests
from packaging import version
from packaging.specifiers import SpecifierSet

# Configure logger
logger = logging.getLogger(__name__)

package_name = "elitea-mcp"
PYPI_URL = f"https://pypi.org/pypi/{package_name}/json"
CHECK_TIMEOUT = (3, 5)
DEFAULT_CHECK_INTERVAL_HOURS = 24
CHECK_FAILURE_REASONS = (
    (requests.exceptions.SSLError, "TLS verification failed for pypi.org (corporate proxy?)"),
    (requests.exceptions.Timeout, "pypi.org did not respond"),
    (requests.exceptions.ConnectionError, "cannot reach pypi.org (no network or DNS failure)"),
)


def get_installed_version() -> Optional[str]:
    """Return the installed elitea-mcp version, or None when running from an uninstalled checkout."""
    try:
        return importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        return None


@dataclass(frozen=True)
class UpdateState:
    current: Optional[str]
    latest: Optional[str] = None
    requires_python: Optional[str] = None
    error: Optional[str] = None

    @property
    def is_update_available(self) -> bool:
        """Tell whether PyPI has a newer release than the one installed."""
        return bool(self.current and self.latest) and version.parse(self.latest) > version.parse(self.current)

    @property
    def is_python_supported(self) -> bool:
        """Tell whether the running interpreter satisfies the latest release's Requires-Python."""
        return not self.requires_python or SpecifierSet(self.requires_python).contains(platform.python_version())

    @property
    def can_update(self) -> bool:
        """Tell whether the one-click update can install the latest release here."""
        return self.is_update_available and self.is_python_supported

    def describe(self) -> str:
        """Phrase the check outcome as the message the tray shows."""
        if self.error:
            return f"Could not check for updates: {self.error}."
        if not self.is_update_available:
            return f"You are on the latest version ({self.current})."
        if not self.is_python_supported:
            return (f"Version {self.latest} requires Python {self.requires_python}; "
                    f"you have {platform.python_version()}.")
        return f"Version {self.latest} is available. You have {self.current}."


def describe_check_failure(error: requests.RequestException) -> str:
    """Turn a failed PyPI request into a short reason a user can act on."""
    if isinstance(error, requests.exceptions.HTTPError) and error.response is not None:
        return f"PyPI returned HTTP {error.response.status_code}"
    for error_type, reason in CHECK_FAILURE_REASONS:
        if isinstance(error, error_type):
            return reason
    return str(error)


def fetch_update_state() -> UpdateState:
    """Ask PyPI for the newest release, keeping the reason when the check fails."""
    current = get_installed_version()
    try:
        response = requests.get(PYPI_URL, timeout=CHECK_TIMEOUT)
        response.raise_for_status()
        info = response.json()["info"]
    except requests.RequestException as error:
        logger.info(f"Could not check for updates: {error}")
        return UpdateState(current=current, error=describe_check_failure(error))
    except (ValueError, KeyError):
        return UpdateState(current=current, error="PyPI returned an unexpected response")
    state = UpdateState(current=current, latest=info["version"], requires_python=info.get("requires_python") or None)
    if state.is_update_available:
        logger.warning(f"New version {state.latest} of {package_name} is available. Current one is {current}.")
    return state


class UpdateChecker:
    """Check PyPI off the UI thread and cache the answer so menu rebuilds never touch the network."""

    def __init__(self, on_change: Callable[[UpdateState], None], interval_hours: float = DEFAULT_CHECK_INTERVAL_HOURS):
        self.state = UpdateState(current=get_installed_version())
        self._on_change = on_change
        self._interval_seconds = interval_hours * 3600
        self._stopped = threading.Event()

    def start(self):
        """Check once now and then every interval, in a background thread."""
        threading.Thread(target=self._check_periodically, name="update-checker", daemon=True).start()

    def stop(self):
        """End the periodic checks."""
        self._stopped.set()

    def refresh(self) -> bool:
        """Fetch the latest state from PyPI and report whether it differs from the cached one."""
        previous, self.state = self.state, fetch_update_state()
        return self.state != previous

    def _check_periodically(self):
        """Refresh until stopped, telling the tray whenever the answer changes; a non-positive interval checks once."""
        while not self._stopped.is_set():
            if self.refresh():
                self._on_change(self.state)
            if self._interval_seconds <= 0:
                return
            self._stopped.wait(self._interval_seconds)
