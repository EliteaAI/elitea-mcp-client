"""Hand what local servers write to stderr to the log on a thread of its own, so a slow log write never blocks a server or a tool call."""
import logging
import os
import queue
import threading
import time

SERVER_OUTPUT_LOGGER_NAME = "elitea_mcp.server_output"
MAX_LINE_BYTES = 8 * 1024
QUEUE_LINES = 10_000
FLUSH_POLL_SECONDS = 0.01

logger = logging.getLogger(SERVER_OUTPUT_LOGGER_NAME)


class ServerOutputForwarder:
    """A bounded queue of server lines and the one thread that logs them; when the log cannot keep up, lines are dropped and counted."""

    def __init__(self):
        self._queue = queue.Queue(QUEUE_LINES)
        self._dropped = {}
        self._lock = threading.Lock()
        self._listener = None

    def submit(self, server_name, line):
        self._start_listener()
        with self._lock:
            dropped = self._dropped.pop(server_name, 0)
        if dropped and not self._offer(server_name, f"... {dropped} lines dropped (logging could not keep up)"):
            self._count_dropped(server_name, dropped + 1)
        elif not self._offer(server_name, line[:MAX_LINE_BYTES]):
            self._count_dropped(server_name, 1)

    def flush(self, timeout):
        """Wait until every queued line is logged, for a clean stop and for tests; False when the time ran out."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(FLUSH_POLL_SECONDS)
        return True

    def _offer(self, server_name, line):
        try:
            self._queue.put_nowait((server_name, line))
        except queue.Full:
            return False
        return True

    def _count_dropped(self, server_name, count):
        with self._lock:
            self._dropped[server_name] = self._dropped.get(server_name, 0) + count

    def _start_listener(self):
        with self._lock:
            if self._listener is None:
                self._listener = threading.Thread(target=self._log_forever, name="server-output", daemon=True)
                self._listener.start()

    def _log_forever(self):
        while True:
            server_name, line = self._queue.get()
            try:
                logger.info("[%s] %s", server_name, line)
            except Exception:
                logging.getLogger(__name__).debug("Could not log a server line", exc_info=True)
            finally:
                self._queue.task_done()


class ServerOutputPipe:
    """The stderr of a server that keeps running between calls: a pipe read line by line on a thread that only reads."""

    def __init__(self, server_name):
        read_fd, write_fd = os.pipe()
        self.server_name = server_name
        self.writer = os.fdopen(write_fd, "wb")
        self._reader = os.fdopen(read_fd, "rb")
        threading.Thread(target=self._read_until_closed, name=f"server-output-{server_name}", daemon=True).start()

    def close_writer(self):
        """Drop this process's copy of the write end once the server holds its own, so the reader ends when the server exits."""
        self.writer.close()

    def _read_until_closed(self):
        with self._reader:
            while chunk := self._reader.readline(MAX_LINE_BYTES):
                line = chunk.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.strip():
                    _forwarder.submit(self.server_name, line)


_forwarder = ServerOutputForwarder()


def forward_server_output(server_name, lines):
    for line in lines:
        _forwarder.submit(server_name, line)


def flush_server_output(timeout=5.0):
    return _forwarder.flush(timeout)
