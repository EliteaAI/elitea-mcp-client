"""The Windows notification area."""
import logging
import os
import subprocess

from .base import TrayPlatform

logger = logging.getLogger(__name__)


class WindowsTray(TrayPlatform):
    logo_file_name = "logo.png"
    clipboard_commands = (["clip"],)

    def child_process_options(self):
        """No console window: the tray's Python is a console program, so a child it starts would open one."""
        return {"creationflags": subprocess.CREATE_NO_WINDOW}

    def format_menu_label(self, text):
        """A Windows menu reads & as the mark of a shortcut key, so a literal one is written twice."""
        return text.replace("&", "&&")

    def open_file(self, path):
        try:
            os.startfile(str(path))
        except OSError as error:
            logger.error(f"Failed to open {path}: {error}")
            logger.info(f"Please manually open: {path}")
