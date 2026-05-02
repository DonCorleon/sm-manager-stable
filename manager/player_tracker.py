"""Live online-player tracker.

Background polling thread that calls EchoPort's `lp` (List_OnlinePlayers)
across every running game-server instance every 30 sec, builds a
{steam_id: {name, x, y, z, level, last_seen}} snapshot, and notifies
subscribers when the snapshot changes.

Used by:
  - /map/api/players/stream SSE endpoint -> live player markers on the
    /map page.
  - (future) discord_integration: instead of doing its own one-off lp
    call on player-join, it could read the cached state from here.

Polling cadence: 30 seconds is a sensible default. EchoPort `lp` is
cheap (~250 bytes per online player; ~50 ms round-trip) so the cost
is trivial. Polling stops automatically when no instances are
running -- a busy populated server pays ~1 KB/min in network noise,
an idle stack pays nothing.

State retention: when a player disappears from the lp output (logged
out / disconnected), they're removed from the snapshot. Frontend
subscribers see the marker go away on the next push.
"""

import logging
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)


# Default poll interval, used while at least one /map SSE client is
# connected. 30s keeps the marker movement smooth.
_DEFAULT_POLL_INTERVAL_SEC = 30.0

# Gate on running instances: if zero are up, sleep a longer interval
# so a stopped manager doesn't dial unreachable EchoPort 120 times/hour.
_IDLE_POLL_INTERVAL_SEC = 60.0

# When NO subscribers are watching, slow the poll right down. The /map
# page is the only consumer; nobody watching = no need for fresh
# state. Each lp call writes a ~6-line block to WS.log so unconditional
# polling pollutes the game-server log fast.
_UNSUBSCRIBED_POLL_INTERVAL_SEC = 600.0


class PlayerTracker:
    """Single-instance background tracker. Use get_tracker() to access
    the module-level singleton -- there's no reason to have multiple
    trackers in one manager process."""

    def __init__(self, poll_interval_sec: float = _DEFAULT_POLL_INTERVAL_SEC):
        self._lock = threading.Lock()
        # state: steam_id -> {name, x, y, z, level, last_seen}
        self._state: dict[str, dict] = {}
        # subscribers: each is an Event() the tracker sets when state changes
        self._subscribers: list[threading.Event] = []
        self._poll_interval = poll_interval_sec
        self._stop = threading.Event()
        # Wakeup nudge: subscribe() sets this so the loop polls
        # immediately on first SSE connect (otherwise the new client
        # would see a stale state up to _UNSUBSCRIBED_POLL_INTERVAL_SEC
        # old, since the tracker had been idle).
        self._wakeup = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ──

    def start(self) -> None:
        """Spin up the background poller. Idempotent."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="player-tracker"
        )
        self._thread.start()
        log.info("player tracker started "
                 "(poll interval=%ss)", self._poll_interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        # Bust the loop out of any wakeup.wait().
        self._wakeup.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    # ── snapshot access ──

    def get_state(self) -> dict[str, dict]:
        """Return a shallow copy of the current state. Safe to iterate
        without holding the tracker's lock."""
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # ── pub/sub for SSE ──

    def subscribe(self) -> threading.Event:
        """Attach a notify-event. Tracker sets it when state changes.
        Caller waits on it, reads get_state(), then clears + repeats.
        Always call unsubscribe() in a finally block to drop the
        reference."""
        event = threading.Event()
        with self._lock:
            self._subscribers.append(event)
        # Nudge the loop so a freshly-attached subscriber gets a poll
        # right away rather than waiting for the unsubscribed cadence
        # to expire.
        self._wakeup.set()
        return event

    def unsubscribe(self, event: threading.Event) -> None:
        with self._lock:
            try:
                self._subscribers.remove(event)
            except ValueError:
                pass

    # ── poll loop ──

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Skip the actual lp call if nobody's watching. This is
            # what made `lp` fire every 30s in WS.log even with the
            # /map page closed -- the tracker poll was unconditional.
            with self._lock:
                has_subs = len(self._subscribers) > 0
            try:
                if has_subs:
                    had_instances = self._poll_once()
                else:
                    had_instances = False
            except Exception:
                log.exception("player tracker poll iteration failed")
                had_instances = False
            if not has_subs:
                interval = _UNSUBSCRIBED_POLL_INTERVAL_SEC
            elif had_instances:
                interval = self._poll_interval
            else:
                interval = _IDLE_POLL_INTERVAL_SEC
            # Wait on stop OR wakeup OR timeout. Whichever fires first.
            # Clear wakeup BEFORE waiting so a set() racing in is not
            # missed; if it set during the prior poll we'll see it
            # immediately and skip the sleep.
            if self._wakeup.is_set():
                self._wakeup.clear()
                continue
            woke = self._wakeup.wait(timeout=interval)
            if woke:
                self._wakeup.clear()
            if self._stop.is_set():
                break

    def _poll_once(self) -> bool:
        """Run one iteration. Returns True if at least one running
        instance was found (regardless of whether players were on)."""
        # Lazy imports so the tracker can be created/imported before
        # the rest of the manager has fully booted.
        try:
            from manager import echo, lifecycle
        except Exception:
            log.exception("player tracker: lifecycle/echo import failed")
            return False

        try:
            running = lifecycle.running_instances()
        except Exception:
            log.exception("player tracker: running_instances() failed")
            return False
        if not running:
            return False

        new_state: dict[str, dict] = {}
        now = time.time()
        # running_instances() yields (RuntimeInstance, InstanceProcess) tuples.
        for ri, _proc in running:
            port = getattr(ri.instance, "echo_port", None)
            if not port:
                continue
            try:
                players = echo.list_online_players("127.0.0.1", port,
                                                     read_timeout=2.0)
            except Exception:
                log.debug("lp call to instance %s failed",
                          getattr(ri.instance, "short_name", "?"),
                          exc_info=True)
                continue
            if not players:
                # Server up but empty (or echoport unreachable -> None).
                # Either way no players to record.
                continue
            level = getattr(ri.instance, "map_name", None)
            for p in players:
                new_state[p["steam_id"]] = {
                    "name": p["name"],
                    "x": p["x"],
                    "y": p["y"],
                    "z": p["z"],
                    "level": level,
                    "last_seen": now,
                }

        with self._lock:
            changed = (new_state != self._state)
            self._state = new_state
            subs = list(self._subscribers) if changed else []

        if changed:
            log.debug("player tracker: %d player(s); notifying %d sub(s)",
                      len(new_state), len(subs))
            for ev in subs:
                ev.set()
        return True


# ── module-level singleton ──


_tracker: Optional[PlayerTracker] = None
_tracker_lock = threading.Lock()


def get_tracker() -> PlayerTracker:
    """Lazy-init + return the singleton tracker. start() it at boot
    from manager.__main__."""
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = PlayerTracker()
        return _tracker


def start_tracker() -> PlayerTracker:
    """Convenience: get-or-create + start. Idempotent."""
    t = get_tracker()
    t.start()
    return t
