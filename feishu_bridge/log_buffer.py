"""Thread-safe ring buffer log handler for Control API."""

import logging
import threading
from collections import deque


class LogRingBuffer(logging.Handler):
    """Thread-safe ring buffer that captures log records for the Control API.

    Attach to the root (or ``feishu-bridge``) logger after ``basicConfig``.
    The ``recent()`` method returns the last *n* entries at or above a
    minimum severity level.
    """

    __slots__ = ("_buffer", "_lock")

    def __init__(self, capacity: int = 2000):
        super().__init__()
        self._buffer: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "ts": record.created,
            "level": record.levelname,
            "msg": self.format(record),
        }
        with self._lock:
            self._buffer.append(entry)

    def recent(self, n: int = 200, level: str = "INFO") -> list[dict]:
        """Return the last *n* entries at or above *level*."""
        min_level = getattr(logging, level.upper(), logging.INFO)
        with self._lock:
            return [
                e
                for e in self._buffer
                if getattr(logging, e["level"], 0) >= min_level
            ][-n:]
