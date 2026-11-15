"""The PID file of a daemon, written only by the process that holds the instance lock, so a refused copy never touches it."""
import atexit
import os
from pathlib import Path


def write_pid_file(path):
    """Record this process for `kill $(cat <file>)` and remove the record when the process exits."""
    Path(path).write_text(f"{os.getpid()}\n")
    atexit.register(remove_pid_file, path)


def remove_pid_file(path):
    try:
        os.remove(path)
    except OSError:
        pass
