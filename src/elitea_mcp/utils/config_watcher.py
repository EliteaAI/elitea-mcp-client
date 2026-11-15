"""Notice that config.json changed, without reading it, so the tray's redraw can decide what the change means."""
import os
import threading


def read_signature(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


class ConfigFileWatcher:
    """Call on_change once a new stat of the file has held for two polls in a row, so a save caught half-written is never read."""

    def __init__(self, path, on_change, interval=2.0):
        self._path = path
        self._on_change = on_change
        self._interval = interval
        self._stopped = threading.Event()

    def start(self):
        threading.Thread(target=self._poll, args=(read_signature(self._path),), name="config-watcher", daemon=True).start()

    def stop(self):
        self._stopped.set()

    def _poll(self, seen):
        pending = seen
        while not self._stopped.wait(self._interval):
            current = read_signature(self._path)
            if current != pending:
                pending = current
            elif current != seen:
                seen = current
                self._on_change()
