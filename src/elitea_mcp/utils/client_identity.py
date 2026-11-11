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
    if not hold_lock(config_dir / CLIENT_ID_LOCK_NAME):
        return f"{machine}-{uuid.uuid4()}"
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


def take_lock(path):
    """Lock the file for as long as the returned handle stays open, or return None when someone else holds it."""
    try:
        handle = open(path, "a+")
    except OSError:
        return None
    try:
        lock_exclusively(handle)
    except OSError:
        handle.close()
        return None
    return handle


def release_lock(handle):
    unlock(handle)
    handle.close()


def is_lock_held_elsewhere(path):
    """Tell whether another handle holds the lock, without keeping it; a lock dies with its holder, even on a crash."""
    if not path.exists():
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
