"""Linux trays: AppIndicator and GTK hosts, and the plain X11 tray pystray falls back to without PyGObject."""
import logging
import subprocess

from .base import TrayPlatform

logger = logging.getLogger(__name__)


class LinuxTray(TrayPlatform):
    clipboard_commands = (["wl-copy"], ["xclip", "-selection", "clipboard"])

    def open_file(self, path):
        try:
            subprocess.run(["xdg-open", str(path)], check=True)
        except (OSError, subprocess.SubprocessError) as error:
            logger.error(f"Failed to open {path}: {error}")
            logger.info(f"Please manually open: {path}")


class XorgTray(LinuxTray):
    def format_title(self, text):
        """The X11 window title is one line, and python-xlib writes it as Latin-1, raising on anything else, so an ellipsis or emoji would stop the tray."""
        return text.replace("\n", " - ").replace("…", "...").encode("latin-1", "replace").decode("latin-1")
