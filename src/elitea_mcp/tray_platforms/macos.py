"""The macOS menu bar."""
import logging
import os
import subprocess

import AppKit
import objc
from Foundation import NSObject
from PIL import Image

from .base import TrayPlatform

logger = logging.getLogger(__name__)

MENU_BAR_APPEARANCES = [AppKit.NSAppearanceNameAqua, AppKit.NSAppearanceNameDarkAqua]
LOGO_ALPHA_THRESHOLD = 50
LOGO_ALPHA_BOOST = 50


def define_appearance_observer():
    """An Objective-C class can be defined only once per process, and this module may be imported under two names."""
    try:
        return objc.lookUpClass("MenuBarAppearanceObserver")
    except objc.nosuchclass_error:
        class MenuBarAppearanceObserver(NSObject):
            """Calls on_change with the status bar button whenever its appearance changes, on the main thread."""

            def observeValueForKeyPath_ofObject_change_context_(self, key_path, button, change, context):
                self.on_change(button)

        return MenuBarAppearanceObserver


MenuBarAppearanceObserver = define_appearance_observer()


def is_dark_appearance(appearance):
    return appearance.bestMatchFromAppearancesWithNames_(MENU_BAR_APPEARANCES) == AppKit.NSAppearanceNameDarkAqua


def fork_safe_environment():
    """Children of a GUI process must skip the Objective-C fork-safety check, or they crash on start."""
    return {**os.environ, "OBJC_DISABLE_INITIALIZE_FORK_SAFETY": "YES"}


def escape_for_applescript(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


class MacOSTray(TrayPlatform):
    clipboard_commands = (["pbcopy"],)

    def __init__(self):
        self.menu_bar_is_dark = True
        self._appearance_observer = None

    def prepare_app(self):
        """Run as a menu bar extra, without a Dock icon."""
        try:
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
            logger.info("Successfully hidden from macOS dock")
        except Exception as error:
            logger.warning(f"Failed to hide from dock: {error}")

    def prepare_logo(self, image):
        """A monochrome logo in the shade the menu bar needs, keeping the logo's greys as alpha for smooth edges."""
        gray = image.convert("L")
        shade = 255 if self.menu_bar_is_dark else 0
        logo = Image.new("RGBA", image.size, (0, 0, 0, 0))
        for x in range(image.width):
            for y in range(image.height):
                gray_value = gray.getpixel((x, y))
                if gray_value > LOGO_ALPHA_THRESHOLD:
                    logo.putpixel((x, y), (shade, shade, shade, min(255, gray_value + LOGO_ALPHA_BOOST)))
        return logo

    def watch_appearance(self, icon, on_change):
        """Follow the menu bar's light or dark look, which tracks the wallpaper as well as the system setting; pystray exposes no hook, so read its status item, keeping the white logo if it is gone."""
        status_item = getattr(icon, "_status_item", None)
        if status_item is None:
            return
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(lambda: self.observe_menu_bar(status_item.button(), on_change))

    def observe_menu_bar(self, button, on_change):
        def follow(changed_button):
            self.menu_bar_is_dark = is_dark_appearance(changed_button.effectiveAppearance())
            on_change()

        self._appearance_observer = MenuBarAppearanceObserver.alloc().init()
        self._appearance_observer.on_change = follow
        button.addObserver_forKeyPath_options_context_(self._appearance_observer, "effectiveAppearance",
                                                       AppKit.NSKeyValueObservingOptionInitial, None)

    def notify(self, icon, title, message):
        """A native notification through osascript; pystray's own is the fallback, on one line, since a multi-line one opens Script Editor."""
        safe_message = escape_for_applescript(message).replace("\n", "\\n")
        script = f'display notification "{safe_message}" with title "{escape_for_applescript(title)}" sound name "default"'
        try:
            subprocess.run(["osascript", "-e", script], check=True, env=fork_safe_environment(), capture_output=True, text=True,
                           start_new_session=True)
        except Exception as error:
            logger.warning(f"Failed to show macOS notification via osascript: {error}")
            super().notify(icon, title, message.replace("\n", " | "))

    def open_file(self, path):
        try:
            subprocess.run(["open", str(path)], check=True, env=fork_safe_environment(), start_new_session=True)
        except (OSError, subprocess.SubprocessError) as error:
            logger.error(f"Failed to open {path}: {error}")
            logger.info(f"Please manually open: {path}")
