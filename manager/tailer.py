"""Log file tailer. Like `tail -f`, but with subscribe/broadcast semantics
so multiple consumers (browser SSE clients, the Discord relay parser, an
audit log writer, etc.) can all see the same line stream from a single
file-watching thread.

One tailer per file. Module-level instances are set up in `setup_tailers()`
and started at process boot. Each tailer:

  - opens the file lazily and re-opens on rotation or truncation
  - keeps a recent-history buffer so newly-attached subscribers see context
  - polls every poll_interval (default 0.5 s -- low enough to feel live,
    high enough that idle CPU stays near 0)

Soulmask rotates server logs on each restart by renaming the existing
WS.log to WS-backup-<timestamp>.log and creating a fresh WS.log. The
tailer detects this via inode change OR a sudden file-size drop.
"""

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Type alias for subscriber callbacks: (timestamp, line) -> None
LineCallback = Callable[[float, str], None]


class LogTailer:
    """Watches a single file. Thread-safe."""

    def __init__(self, path: Path, name: str,
                 history_size: int = 500,
                 poll_interval: float = 0.5):
        self.path = Path(path)
        self.name = name  # short id used in log lines and the SSE URL
        self.history_size = history_size
        self.poll_interval = poll_interval

        self._history: deque[tuple[float, str]] = deque(maxlen=history_size)
        self._subscribers: list[LineCallback] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        log.info("LogTailer[%s] starting on %s", self.name, self.path)
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"tailer-{self.name}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    # ── Subscriber API ──────────────────────────────────────────────────────

    def subscribe(self, callback: LineCallback) -> Callable[[], None]:
        """Register a callback that fires for every new line. Returns an
        unsubscribe function -- call it when the consumer goes away (e.g.
        SSE client disconnects)."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)
        return unsubscribe

    def get_history(self) -> list[tuple[float, str]]:
        """Snapshot of buffered recent lines. Used by SSE clients on connect
        so they see context immediately instead of waiting for new lines."""
        with self._lock:
            return list(self._history)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # ── Internal ────────────────────────────────────────────────────────────

    def _emit(self, ts: float, line: str) -> None:
        """Append to history and fan out to all subscribers."""
        with self._lock:
            self._history.append((ts, line))
            subs = list(self._subscribers)
        # Subscribers' callbacks run with no lock so they can do IO etc.
        for cb in subs:
            try:
                cb(ts, line)
            except Exception:
                log.exception("LogTailer[%s] subscriber callback raised", self.name)

    def _loop(self) -> None:
        f = None
        inode = None  # (st_ino, st_dev) of the currently-open file

        while not self._stop_event.is_set():
            try:
                if not self.path.exists():
                    # File doesn't exist yet (e.g. server hasn't started and
                    # WS.log isn't created yet). Close any stale handle and
                    # poll less often.
                    if f is not None:
                        f.close()
                        f = None
                        inode = None
                    self._stop_event.wait(self.poll_interval * 4)
                    continue

                stat = self.path.stat()
                current_inode = (stat.st_ino, stat.st_dev)

                # Detect rotation or truncation while we have an open handle.
                if f is not None:
                    rotated = inode is not None and current_inode != inode
                    truncated = stat.st_size < f.tell()
                    if rotated or truncated:
                        log.info("LogTailer[%s] %s -- reopening",
                                 self.name, "rotated" if rotated else "truncated")
                        f.close()
                        f = None

                if f is None:
                    f = self.path.open("r", encoding="utf-8", errors="replace")
                    if inode is None:
                        # First open in this process. Seek to END so we don't
                        # flood subscribers with the entire historical log.
                        f.seek(0, 2)
                        log.info("LogTailer[%s] opened %s (size=%d, "
                                 "seek to end for live tailing)",
                                 self.name, self.path, stat.st_size)
                    else:
                        # Reopen after rotation: file is fresh, read from start.
                        log.info("LogTailer[%s] reopened %s after rotation "
                                 "(reading from beginning of new file)",
                                 self.name, self.path)
                    inode = current_inode

                # Drain any new lines.
                while True:
                    line = f.readline()
                    if not line:
                        break
                    line = line.rstrip("\r\n")
                    if line:
                        self._emit(time.time(), line)

                self._stop_event.wait(self.poll_interval)
            except Exception:
                log.exception("LogTailer[%s] iteration crashed", self.name)
                self._stop_event.wait(self.poll_interval * 4)

        if f is not None:
            try:
                f.close()
            except Exception:
                pass
        log.info("LogTailer[%s] stopped", self.name)


# ── Module-level registry ───────────────────────────────────────────────────


# Stable ordering of tabs in the UI. The "events" entry is a virtual tailer
# (manager.events.EventStream) that aggregates parsed game events from the
# ws and ws_2 file tailers; it doesn't tail a file itself but exposes the
# same subscribe / get_history interface so /logs SSE serves it unchanged.
TAILER_NAMES = ["manager", "ws", "ws_2", "events"]

_tailers: dict[str, LogTailer] = {}
_setup_lock = threading.Lock()
_setup_done = False


def get_tailer(name: str) -> Optional[LogTailer]:
    return _tailers.get(name)


def get_all_tailers() -> dict[str, LogTailer]:
    """Tailer registry in display order."""
    return {n: _tailers[n] for n in TAILER_NAMES if n in _tailers}


def get_friendly_labels() -> dict[str, str]:
    """User-friendly labels for tabs. Computed at call time so flipping
    main_map in the wizard updates the labels on the next page render."""
    try:
        from manager.config import load_settings
        from manager.wizard import (
            MAP_CLOUDMIST, MAP_SHIFTINGSANDS, MAPS, load_existing,
        )
        config = load_existing(load_settings())
        labels: dict[str, str] = {"manager": "Manager"}
        if config.mode == "cluster":
            main_lbl = MAPS[config.main_map]
            other_map = (MAP_SHIFTINGSANDS if config.main_map == MAP_CLOUDMIST
                         else MAP_CLOUDMIST)
            other_lbl = MAPS[other_map]
            labels["ws"] = f"{main_lbl} (main)"
            labels["ws_2"] = f"{other_lbl} (child)"
        else:
            labels["ws"] = MAPS.get(config.main_map, "WS.log")
            labels["ws_2"] = "(unused in single mode)"
        labels["events"] = "Game events"
        return labels
    except Exception:
        log.exception("get_friendly_labels: failed, using fallback")
        return {"manager": "Manager", "ws": "WS.log",
                "ws_2": "WS_2.log", "events": "Game events"}


def setup_tailers() -> None:
    """Create and start the standard tailers. Idempotent -- calling twice
    is a no-op. Called once from __main__ at process boot."""
    global _setup_done
    with _setup_lock:
        if _setup_done:
            return

        from manager.logging_setup import LOGS_DIR
        from manager.paths import server_log_path

        manager_log = LOGS_DIR / "manager.log"
        ws_log = server_log_path(secondary=False)   # WS.log
        ws_2_log = server_log_path(secondary=True)  # WS_2.log

        _tailers["manager"] = LogTailer(manager_log, "manager")
        _tailers["ws"] = LogTailer(ws_log, "ws")
        _tailers["ws_2"] = LogTailer(ws_2_log, "ws_2")

        for t in _tailers.values():
            t.start()

        # Set up the parsed-events stream AFTER the file tailers exist so
        # it can attach. EventStream is duck-typed compatible with LogTailer
        # (subscribe / get_history / subscriber_count), so adding it to the
        # registry under the "events" key lets the same SSE endpoint serve
        # it as a 4th tab in /logs.
        from manager.events import setup_event_stream
        _tailers["events"] = setup_event_stream()

        _setup_done = True
        log.info("Tailers initialised: %s", list(_tailers.keys()))
