"""The tray's platform-specific behaviour, one object per kind of tray."""
import platform

import pystray

from .base import TrayPlatform
from .linux import LinuxTray, XorgTray
from .windows import WindowsTray

TRAYS_BY_PYSTRAY_BACKEND = {
    "_win32": WindowsTray,
    "_appindicator": LinuxTray,
    "_gtk": LinuxTray,
    "_xorg": XorgTray,
}

if platform.system() == "Darwin":
    from .macos import MacOSTray
    TRAYS_BY_PYSTRAY_BACKEND["_darwin"] = MacOSTray


def get_tray_platform():
    """Chosen from the backend pystray picked, not the OS, because on Linux the AppIndicator and X11 trays behave differently."""
    backend = pystray.Icon.__module__.rsplit(".", 1)[-1]
    return TRAYS_BY_PYSTRAY_BACKEND.get(backend, TrayPlatform)()
