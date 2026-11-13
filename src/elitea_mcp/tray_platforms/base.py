"""What the tray needs from the platform it runs on, with the behaviour every platform shares."""
import logging
import subprocess

logger = logging.getLogger(__name__)


class TrayPlatform:
    logo_file_name = "logo64x64.png"
    clipboard_commands = ()

    def prepare_app(self):
        """Set up the process before the icon is created."""

    def prepare_logo(self, image):
        """The logo as this platform's tray shows it best."""
        return image

    def watch_appearance(self, icon, on_change):
        """Call on_change whenever the tray's look changes in a way the logo has to follow."""

    def format_title(self, text):
        return text

    def format_menu_label(self, text):
        return text

    def notify(self, icon, title, message):
        if icon:
            icon.notify(message, title)

    def open_file(self, path):
        logger.warning(f"Opening files is not supported here. Please manually open: {path}")

    def copy_to_clipboard(self, text):
        """Put text on the clipboard with the platform's own tool, reporting whether it worked."""
        for command in self.clipboard_commands:
            try:
                subprocess.run(command, input=text, text=True, check=True, timeout=5)
                return True
            except (OSError, subprocess.SubprocessError, UnicodeError):
                continue
        return False
