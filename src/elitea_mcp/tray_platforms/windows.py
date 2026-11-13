"""The Windows notification area."""
import logging
import os

from .base import TrayPlatform

logger = logging.getLogger(__name__)


class WindowsTray(TrayPlatform):
    logo_file_name = "logo.png"
    clipboard_commands = (["clip"],)

    def format_menu_label(self, text):
        """A Windows menu reads & as the mark of a shortcut key, so a literal one is written twice."""
        return text.replace("&", "&&")

    def open_file(self, path):
        try:
            os.startfile(str(path))
        except OSError as error:
            logger.error(f"Failed to open {path}: {error}")
            logger.info(f"Please manually open: {path}")
