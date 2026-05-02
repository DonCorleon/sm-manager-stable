"""Tiny pub-sub for waking dashboard SSE subscribers on state changes.

Each subscriber holds a `threading.Event`. When something interesting
happens (server start/stop, login-lock toggle, backup op begin/end,
cancel-shutdown), the producer calls `notify()` which sets every
subscriber's event. The SSE generator waits on its event with a
timeout (the refresh cadence), then clears the event and renders a
fresh status frame.

Net effect: dashboard gets timer-cadence updates AT MINIMUM, plus
instant push on rare events. No constant per-request churn.
"""

import logging
import threading

log = logging.getLogger(__name__)

_subscribers: list[threading.Event] = []
_lock = threading.Lock()


def subscribe() -> threading.Event:
    """Register a new subscriber. Returns the Event the subscriber should
    wait() on. Callers MUST eventually call unsubscribe(event) so we don't
    leak entries when browsers disconnect."""
    e = threading.Event()
    with _lock:
        _subscribers.append(e)
        n = len(_subscribers)
    log.debug("dashboard_events: subscriber added (now %d)", n)
    return e


def unsubscribe(event: threading.Event) -> None:
    with _lock:
        if event in _subscribers:
            _subscribers.remove(event)
            n = len(_subscribers)
            log.debug("dashboard_events: subscriber removed (now %d)", n)


def notify() -> None:
    """Wake every subscriber. They re-render and clear their event."""
    with _lock:
        n = len(_subscribers)
        for e in _subscribers:
            e.set()
    if n:
        log.debug("dashboard_events: notified %d subscriber(s)", n)


def subscriber_count() -> int:
    with _lock:
        return len(_subscribers)
