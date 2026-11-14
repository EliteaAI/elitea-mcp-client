"""The Settings window: a small Tk form over settings_form, run as its own process so the tray never waits on it."""
import logging
import queue
import sys
import threading
from importlib import resources

from .config_errors import ConfigError
from .settings_form import SettingsExit, check_connection, describe_stored_token, find_field_errors, load_form, save_form

logger = logging.getLogger(__name__)

FIELD_LABELS = (("deployment_url", "Deployment URL"), ("auth_token", "Token"), ("project_id", "Project ID"))


def run_settings_window():
    """Show the window until it closes and return the exit code the tray acts on."""
    try:
        import tkinter
        from tkinter import ttk
    except ImportError:
        return SettingsExit.TK_MISSING
    try:
        root = tkinter.Tk()
    except tkinter.TclError as error:
        logger.error(f"Settings cannot open a window: {error}")
        return SettingsExit.NO_DISPLAY
    return SettingsWindow(tkinter, ttk, root, load_form()).run()


def show_in_dock_as_elitea():
    """The window's Dock tile shows the ELITEA logo instead of Python's, and asks to be the active app."""
    import AppKit
    application = AppKit.NSApplication.sharedApplication()
    logo = AppKit.NSImage.alloc().initWithContentsOfFile_(str(resources.files("elitea_mcp.icons") / "logo64x64.png"))
    if logo is not None:
        application.setApplicationIconImage_(logo)
    application.activateIgnoringOtherApps_(True)


class SettingsWindow:
    def __init__(self, tkinter, ttk, root, form):
        self.tkinter, self.ttk, self.root, self.form = tkinter, ttk, root, form
        self.exit_code = SettingsExit.CANCELLED
        self.test_results = queue.Queue()
        self.values = {name: tkinter.StringVar(root, value=getattr(form, name)) for name, _ in FIELD_LABELS}
        self.field_errors = {name: tkinter.StringVar(root) for name, _ in FIELD_LABELS}
        self.show_token = tkinter.BooleanVar(root, value=False)
        self.restart = tkinter.BooleanVar(root, value=True)
        self.status = tkinter.StringVar(root)
        self.build()

    def build(self):
        """Lay out the three fields, the test results and the buttons."""
        ttk, root = self.ttk, self.root
        root.title("ELITEA MCP Client Settings")
        root.resizable(False, False)
        frame = ttk.Frame(root, padding=16)
        frame.grid(sticky="nsew")
        row = 0
        for name, label in FIELD_LABELS:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=(6, 0))
            entry = ttk.Entry(frame, textvariable=self.values[name], width=44, show="•" if name == "auth_token" else "")
            entry.grid(row=row, column=1, sticky="ew", pady=(6, 0))
            if name == "auth_token":
                self.token_entry = entry
                row += 1
                hint = ttk.Frame(frame)
                hint.grid(row=row, column=1, sticky="ew")
                ttk.Label(hint, text=describe_stored_token(self.form), foreground="gray").pack(side="left")
                ttk.Checkbutton(hint, text="Show", variable=self.show_token, command=self.toggle_token).pack(side="right")
            row += 1
            ttk.Label(frame, textvariable=self.field_errors[name], foreground="#c62828").grid(row=row, column=1, sticky="w")
            row += 1
        self.test_button = ttk.Button(frame, text="Test connection", command=self.start_test)
        self.test_button.grid(row=row, column=1, sticky="w", pady=(8, 4))
        row += 1
        self.results = ttk.Label(frame, text="", justify="left", wraplength=380)
        self.results.grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        ttk.Checkbutton(frame, text="Restart the MCP servers if they are running", variable=self.restart).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        row += 1
        ttk.Label(frame, textvariable=self.status, foreground="#c62828", wraplength=380, justify="left").grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right", padx=(0, 8))
        root.protocol("WM_DELETE_WINDOW", self.cancel)
        root.bind("<Escape>", lambda event: self.cancel())

    def run(self):
        """Bring the window to the front, wait until it closes and return the exit code."""
        if sys.platform == "darwin":
            show_in_dock_as_elitea()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()
        self.root.mainloop()
        return self.exit_code

    def toggle_token(self):
        self.token_entry.configure(show="" if self.show_token.get() else "•")

    def read_form(self):
        """The form with the typed values, keeping the saved token and TLS setting it was loaded with."""
        for name, _ in FIELD_LABELS:
            setattr(self.form, name, self.values[name].get())
        return self.form

    def check_fields(self):
        """Show each field's problem under it and report whether the form can be used."""
        errors = find_field_errors(self.read_form())
        for name, _ in FIELD_LABELS:
            self.field_errors[name].set(errors.get(name, ""))
        return not errors

    def start_test(self):
        """Run the platform checks on a daemon thread, so closing the window during a slow test ends this process at once."""
        if not self.check_fields():
            return
        self.test_button.state(["disabled"])
        self.results.configure(text="Testing…")
        form = self.read_form()
        threading.Thread(target=lambda: self.test_results.put(check_connection(form)), name="connection-test",
                         daemon=True).start()
        self.root.after(100, self.show_test_results)

    def show_test_results(self):
        try:
            lines = self.test_results.get_nowait()
        except queue.Empty:
            self.root.after(100, self.show_test_results)
            return
        self.results.configure(text="\n".join(f"{ {True: '✓', False: '✗', None: '•'}[line.passed]} {line.title}: {line.text}" for line in lines))
        self.test_button.state(["!disabled"])

    def save(self):
        """Write the connection settings and close, or say under the buttons why the file was left as it is."""
        if not self.check_fields():
            return
        try:
            save_form(self.read_form())
        except (ConfigError, OSError) as error:
            logger.error(f"Settings were not saved: {error}")
            self.status.set(f"Not saved. {error}")
            return
        self.exit_code = SettingsExit.SAVED_RESTART if self.restart.get() else SettingsExit.SAVED
        self.root.destroy()

    def cancel(self):
        self.exit_code = SettingsExit.CANCELLED
        self.root.destroy()
