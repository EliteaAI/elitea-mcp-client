"""The About window: which client and version this is, in a small Tk window of its own process, since the system can hide a notification without a trace."""
import logging
from importlib import resources

from .settings_form import SettingsExit
from .settings_window import bring_to_front
from .utils.package_updates import get_installed_version

logger = logging.getLogger(__name__)


def describe_about():
    """The lines About shows, in the window or, when none can open, in a notification or the terminal."""
    version = get_installed_version() or "unknown (development checkout)"
    return (f"Version: {version}", "Model Context Protocol client for the ELITEA platform.", "Licensed under the Apache License 2.0.")


def run_about_window():
    """Show the window until it closes and return the exit code the tray acts on, printing the text instead when no window can open."""
    try:
        import tkinter
        from tkinter import ttk
    except ImportError:
        print("\n".join(describe_about()))
        return SettingsExit.TK_MISSING
    try:
        root = tkinter.Tk()
    except tkinter.TclError as error:
        logger.error(f"About cannot open a window: {error}")
        print("\n".join(describe_about()))
        return SettingsExit.NO_DISPLAY
    build_about_window(tkinter, ttk, root)
    bring_to_front(root)
    root.mainloop()
    return SettingsExit.CANCELLED


def build_about_window(tkinter, ttk, root):
    """Lay out the logo, the name, the About lines and a Close button that Escape and Enter also press."""
    root.title("About ELITEA MCP Client")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=20)
    frame.grid(sticky="nsew")
    root.logo = tkinter.PhotoImage(master=root, file=str(resources.files("elitea_mcp.icons") / "logo.png"))
    ttk.Label(frame, image=root.logo).grid(row=0, column=0, rowspan=4, sticky="n", padx=(0, 16))
    ttk.Label(frame, text="ELITEA MCP Client", font=("TkDefaultFont", 14, "bold")).grid(row=0, column=1, sticky="w")
    for row, line in enumerate(describe_about(), start=1):
        ttk.Label(frame, text=line).grid(row=row, column=1, sticky="w", pady=(4, 0))
    close = ttk.Button(frame, text="Close", command=root.destroy)
    close.grid(row=4, column=1, sticky="e", pady=(16, 0))
    root.bind("<Escape>", lambda event: root.destroy())
    root.bind("<Return>", lambda event: root.destroy())
    close.focus_set()
