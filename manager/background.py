"""Generic background-task queue.

A single daemon worker thread drains a bounded queue of (name, callable,
args, kwargs) tuples and runs each one. Used to push heavy synchronous
work off threads that need to stay responsive -- specifically:

  - The WS.log tailer thread (which fans out events to subscribers).
    Anything slow done inside an event-handler callback stalls every
    other subscriber waiting in line.
  - Flask request-handler threads (POSTs that kick off git pull /
    pip install / steamcmd should return immediately and let the
    operator watch progress on a streaming endpoint).

Pattern:

    from manager import background
    background.submit("discord-join-map", _send_join_map_impl, event)

The worker is started exactly once at boot via `background.start()`.
Submitting before start is safe -- the queue just buffers until the
worker drains it.

Drop-on-full: if the queue is saturated (200 items pending) the new
submission is dropped and a WARNING is logged. This bounds memory
when something downstream stalls; never let a producer accidentally
OOM the manager.

Failures inside a task are caught and logged; the worker keeps
running. Tasks should NOT raise to communicate state -- if you need
a result, write it to a shared structure or pass a callback.

(Also used by /updates as the executor for Apply / Check / etc., and
by discord_integration for the join-map render.)
"""

import logging
import queue
import threading
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# Bound the queue so a stalled downstream (Discord 5xx, slow disk)
# can't accumulate work indefinitely. 200 items is generous for the
# expected event rate.
_QUEUE_MAX = 200

_q: "queue.Queue[tuple[str, Callable[..., Any], tuple, dict]]" = (
    queue.Queue(maxsize=_QUEUE_MAX)
)
_worker: Optional[threading.Thread] = None
_dropped = 0
_dropped_lock = threading.Lock()


def submit(name: str, func: Callable[..., Any], *args, **kwargs) -> bool:
    """Enqueue heavy work. Never blocks. Returns True if the task was
    queued, False if the queue was full and the task was dropped.

    `name` is a short identifier used in log lines so failures can
    be traced back to their submission site. Pick something specific
    (e.g. "discord-join-map", not "task")."""
    global _dropped
    try:
        _q.put_nowait((name, func, args, kwargs))
        return True
    except queue.Full:
        with _dropped_lock:
            _dropped += 1
        # Throttle the warning: log every 10th drop so a sustained
        # storm doesn't flood manager.log on its own.
        if _dropped % 10 == 1:
            log.warning(
                "background queue full (max=%d); dropping %r "
                "(%d total dropped since boot)",
                _QUEUE_MAX, name, _dropped,
            )
        return False


def dropped_count() -> int:
    """For observability: total tasks dropped due to queue saturation
    since boot."""
    with _dropped_lock:
        return _dropped


def queue_depth() -> int:
    """Approximate count of pending tasks. Reads queue.qsize, which
    is approximate but lock-free."""
    return _q.qsize()


def _loop() -> None:
    log.info("background worker started")
    while True:
        try:
            name, func, args, kwargs = _q.get()
        except Exception:
            # queue.get() shouldn't raise on a daemon shutdown, but
            # be defensive -- the only way out of this loop is the
            # process dying.
            log.exception("background queue.get() raised")
            continue
        try:
            log.debug("background: running %r", name)
            func(*args, **kwargs)
        except Exception:
            log.exception("background task %r raised", name)
        finally:
            try:
                _q.task_done()
            except Exception:
                pass


def start() -> None:
    """Idempotent: spin up the worker if not already running."""
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    _worker = threading.Thread(
        target=_loop, daemon=True, name="bg-worker"
    )
    _worker.start()
