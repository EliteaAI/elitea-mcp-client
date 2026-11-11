"""Who this client is to the platform: a durable, host-bound id plus a per-process epoch and generation counter."""
import hashlib
import itertools
import logging
import os
import platform
import time
import uuid

from ..config import get_config_dir, write_file_atomically

try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt

CLIENT_ID_FILE_NAME = "client_id"
CLIENT_ID_LOCK_NAME = "client_id.lock"
SESSION_STATUS_FILE_NAME = "status.json"
MACHINE_PART_LENGTH = 12
RANDOM_NODE_BIT = 1 << 40
_held_locks = {}

logger = logging.getLogger(__name__)


def read_machine_part():
    """Tie the id to this host; uuid.getnode() falls back to a random number per process when it finds no MAC address."""
    node = uuid.getnode()
    hardware = "" if node & RANDOM_NODE_BIT else str(node)
    return hashlib.sha256(f"{platform.node()}|{hardware}".encode()).hexdigest()[:MACHINE_PART_LENGTH]


def load_client_id():
    """The id stored next to config.json; a second running copy gets a throwaway one, so it never takes the first one's servers."""
    config_dir = get_config_dir()
    path = config_dir / CLIENT_ID_FILE_NAME
    machine = read_machine_part()
    newly_held = not holds_client_lock()
    if not hold_lock(get_client_lock_path()):
        return f"{machine}-{uuid.uuid4()}"
    if newly_held:
        clear_previous_session_status()
    try:
        stored = path.read_text(encoding="utf-8").strip()
    except OSError:
        stored = ""
    if stored.startswith(f"{machine}-"):
        return stored
    client_id = f"{machine}-{uuid.uuid4()}"
    try:
        write_file_atomically(path, client_id)
    except OSError as error:
        logger.warning(f"Could not save the client id to {path}: {error}")
    return client_id


def clear_previous_session_status():
    """A new lock holder starts publishing only once connected, so the status a previous holder left must not pass for this one's."""
    path = get_client_lock_path().parent / SESSION_STATUS_FILE_NAME
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning(f"Could not remove the previous session status {path}: {error}")


def hold_lock(path):
    """Lock the file for the rest of this process; the system releases it when the process ends, even on a crash."""
    if str(path) in _held_locks:
        return True
    try:
        handle = open(path, "a+")
    except OSError as error:
        logger.warning(f"Could not open {path}: {error}")
        return False
    try:
        lock_exclusively(handle)
    except OSError:
        handle.close()
        return False
    _held_locks[str(path)] = handle
    return True


def lock_exclusively(handle):
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


def get_client_lock_path():
    return get_config_dir() / CLIENT_ID_LOCK_NAME


def holds_client_lock():
    """Tell whether this process is the client connected with this config directory."""
    return str(get_client_lock_path()) in _held_locks


def is_client_lock_held_elsewhere():
    """Tell whether another process holds the client lock, without keeping it; the lock dies with its holder, even on a crash."""
    path = get_client_lock_path()
    if str(path) in _held_locks or not path.exists():
        return False
    try:
        handle = open(path, "a+")
    except OSError:
        return False
    with handle:
        try:
            lock_exclusively(handle)
        except OSError:
            return True
        unlock(handle)
    return False


def unlock(handle):
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    else:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def create_time_ordered_uuid():
    """A version 7 UUID: its text sorts by creation time, so the platform can tell a newer process from an older one."""
    value = (time.time_ns() // 1_000_000) << 80 | int.from_bytes(os.urandom(10), "big")
    value = (value & ~(0xF << 76)) | (0x7 << 76)
    value = (value & ~(0x3 << 62)) | (0x2 << 62)
    return str(uuid.UUID(int=value))


EPOCH = create_time_ordered_uuid()
_generations = itertools.count(1)


def take_next_generation():
    return next(_generations)
