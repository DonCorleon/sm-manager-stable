"""Backup schedule state machine.

Three independent triggers (each toggleable + interval-configurable):

  ONLINE cadence    -- N hours while at least one player connected.
  POST-LOGOFF       -- one-shot N minutes after server transitions to empty.
  OFFLINE cadence   -- N hours while server is empty (default off).

State transitions:

    [online]  -- player count drops to 0   --> [pending post-logoff]
    [pending] -- delay elapsed              --> [save once, then offline]
    [pending] -- player reconnects          --> [online]
    [offline] -- player connects            --> [online]
    [offline] -- timer elapses              --> [save, stay offline]
    [online]  -- timer elapses              --> [save, stay online]

Source values written on the resulting snapshot:
  scheduled-online, scheduled-offline, post-logoff.

Polls every POLL_SEC and reads player count via `lifecycle.get_status()`
(Steam A2S query already cached there). Logged transitions land in
manager.log so the operator can see when the state machine flipped.
"""

import logging
import threading
import time
from typing import Optional

from manager import backups, broadcasts, lifecycle
from manager.config import get_setting

log = logging.getLogger(__name__)

# How often the scheduler wakes up and re-evaluates state. 15s is tight
# enough to catch the T-30sec broadcast window for scheduled saves
# without waking the box too often.
POLL_SEC = 15

# Broadcast warning lead times (seconds). Longest first.
_BROADCAST_LEAD_SECONDS = (180, 30)


def _truthy(v) -> bool:
    """Settings are stored as 'on'/'off' strings via the choice fields, but
    older / hand-edited values may be bools. Accept either."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("on", "true", "1", "yes")


def _online_enabled() -> bool:
    return _truthy(get_setting("backups.schedule_online_enabled", True))


def _online_interval_sec() -> int:
    return int(get_setting("backups.schedule_online_interval_hours", 2)) * 3600


def _offline_enabled() -> bool:
    return _truthy(get_setting("backups.schedule_offline_enabled", False))


def _offline_interval_sec() -> int:
    return int(get_setting("backups.schedule_offline_interval_hours", 6)) * 3600


def _post_logoff_enabled() -> bool:
    return _truthy(get_setting("backups.post_logoff_save_enabled", True))


def _post_logoff_delay_sec() -> int:
    return int(get_setting("backups.post_logoff_delay_minutes", 45)) * 60


def _broadcasts_enabled() -> bool:
    return _truthy(get_setting("backups.broadcast_warnings_enabled", True))


def _connected_players() -> int:
    """Sum of players across all running instances. 0 if nothing's running
    or queries are timing out."""
    try:
        s = lifecycle.get_status()
    except Exception:
        log.exception("scheduler: get_status() raised")
        return 0
    total = 0
    for inst in s.get("instances", []):
        if not inst.get("running"):
            continue
        q = inst.get("query") or {}
        total += int(q.get("players", 0) or 0)
    return total


def _any_running() -> bool:
    try:
        return bool(lifecycle.running_instances())
    except Exception:
        log.exception("scheduler: running_instances() raised")
        return False


# ── Scheduler ───────────────────────────────────────────────────────────────


class BackupScheduler:
    """Singleton-ish state holder. Started once from __main__."""

    def __init__(self) -> None:
        # state ∈ {"idle", "online", "pending", "offline"}
        # "idle" = no servers running; reset state.
        self.state: str = "idle"
        self.last_online_save_at: Optional[float] = None    # monotonic
        self.last_offline_save_at: Optional[float] = None
        self.pending_started_at: Optional[float] = None
        # Broadcast dedupe: maps (cadence, lead_seconds) -> the next_due_at
        # value the warning was last fired for. Cleared implicitly when
        # next_due_at advances after the snapshot.
        self._warn_sent: dict[tuple[str, int], float] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()  # serialise tick() vs external reads

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            log.warning("BackupScheduler.start: already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="backup-scheduler",
        )
        self._thread.start()
        log.info("BackupScheduler started (poll=%ds)", POLL_SEC)

    def stop(self) -> None:
        log.info("BackupScheduler stopping")
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=POLL_SEC + 5)

    # ── inspection (for /backups debug, future use) ────────────────────────

    def snapshot_state(self) -> dict:
        """Read-only state dump for diagnostics."""
        with self._lock:
            now = time.monotonic()
            return {
                "state": self.state,
                "online_enabled": _online_enabled(),
                "online_interval_sec": _online_interval_sec(),
                "online_due_in_sec": (
                    int(self.last_online_save_at + _online_interval_sec() - now)
                    if self.state == "online" and self.last_online_save_at else None
                ),
                "offline_enabled": _offline_enabled(),
                "offline_interval_sec": _offline_interval_sec(),
                "offline_due_in_sec": (
                    int((self.last_offline_save_at or now) +
                        _offline_interval_sec() - now)
                    if self.state == "offline" and _offline_enabled() else None
                ),
                "post_logoff_enabled": _post_logoff_enabled(),
                "post_logoff_delay_sec": _post_logoff_delay_sec(),
                "post_logoff_due_in_sec": (
                    int(self.pending_started_at + _post_logoff_delay_sec() - now)
                    if self.state == "pending" and self.pending_started_at else None
                ),
            }

    # ── inner loop ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        # Wait briefly before first tick so the manager fully boots
        # (lifecycle's adopted-process discovery takes a moment).
        if self._stop.wait(timeout=15):
            return
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("scheduler: tick raised")
            if self._stop.wait(timeout=POLL_SEC):
                return

    def _tick(self) -> None:
        with self._lock:
            self._tick_locked()

    def _tick_locked(self) -> None:
        log.verbose("scheduler tick: state=%s last_online=%s last_offline=%s "
                    "pending_started=%s",
                    self.state, self.last_online_save_at,
                    self.last_offline_save_at, self.pending_started_at)
        if not _any_running():
            if self.state != "idle":
                log.info("scheduler: no servers running -- state idle "
                         "(was %s)", self.state)
                self._reset_state()
            return

        n = _connected_players()
        now = time.monotonic()
        log.verbose("scheduler tick: any_running=true players=%d", n)

        # ── State transitions ─────────────────────────────────────────────
        if self.state == "idle":
            # Initial state at boot when servers are running. Decide
            # online vs offline based on player count. Don't immediately
            # fire -- baseline the cadence timer at "now".
            if n > 0:
                log.info("scheduler: idle -> online (players=%d at boot)", n)
                self.state = "online"
                self.last_online_save_at = now
            else:
                log.info("scheduler: idle -> offline (no players at boot)")
                self.state = "offline"
                self.last_offline_save_at = now
            return  # no fire on the boot transition

        if n > 0 and self.state in ("offline", "pending"):
            log.info("scheduler: %s -> online (players=%d)", self.state, n)
            # Reset cadence baseline so we don't fire immediately on join.
            self.state = "online"
            self.last_online_save_at = now
            self.pending_started_at = None
            return

        if n == 0 and self.state == "online":
            log.info("scheduler: online -> pending (last player disconnected)")
            self.state = "pending"
            self.pending_started_at = now
            return

        # ── Cadence firing decisions ──────────────────────────────────────
        if self.state == "online" and _online_enabled():
            interval = _online_interval_sec()
            next_due = (self.last_online_save_at or now) + interval
            self._maybe_broadcast_warning("online", next_due, now)
            if now >= next_due:
                log.info("scheduler: online cadence reached "
                         "(elapsed %ds, interval %ds) -- firing snapshot",
                         int(now - (self.last_online_save_at or now)),
                         interval)
                self._fire("scheduled-online")
                self.last_online_save_at = now

        elif self.state == "pending" and _post_logoff_enabled():
            delay = _post_logoff_delay_sec()
            next_due = (self.pending_started_at or now) + delay
            # Post-logoff broadcasts are a no-op when no players online,
            # which is the only state this branch fires in. The chat log
            # still records the say for audit.
            self._maybe_broadcast_warning("pending", next_due, now)
            if now >= next_due:
                log.info("scheduler: post-logoff delay reached "
                         "(elapsed %ds, delay %ds) -- firing one-shot save",
                         int(now - (self.pending_started_at or now)), delay)
                self._fire("post-logoff")
                # After post-logoff fires, transition to offline cadence.
                self.state = "offline"
                self.last_offline_save_at = now
                self.pending_started_at = None

        elif self.state == "offline" and _offline_enabled():
            interval = _offline_interval_sec()
            next_due = (self.last_offline_save_at or now) + interval
            self._maybe_broadcast_warning("offline", next_due, now)
            if now >= next_due:
                log.info("scheduler: offline cadence reached "
                         "(elapsed %ds, interval %ds) -- firing snapshot",
                         int(now - (self.last_offline_save_at or now)),
                         interval)
                self._fire("scheduled-offline")
                self.last_offline_save_at = now

    def _reset_state(self) -> None:
        self.state = "idle"
        self.last_online_save_at = None
        self.last_offline_save_at = None
        self.pending_started_at = None
        self._warn_sent.clear()

    def _maybe_broadcast_warning(self, cadence: str, next_due: float,
                                 now: float) -> None:
        """Fire T-3min and T-30sec broadcasts as the snapshot approaches.
        Each (cadence, lead) pair fires once per next_due value -- once
        the snapshot fires, next_due advances and the gates re-arm.
        """
        if not _broadcasts_enabled():
            return
        secs_until = next_due - now
        for lead in _BROADCAST_LEAD_SECONDS:
            if 0 < secs_until <= lead:
                key = (cadence, lead)
                if self._warn_sent.get(key) != next_due:
                    log.info("scheduler: T-%d broadcast for %s cadence "
                             "(secs_until=%.0f)", lead, cadence, secs_until)
                    try:
                        broadcasts.warn_pre_save(int(secs_until))
                    except Exception:
                        log.exception("broadcast: warn_pre_save raised")
                    self._warn_sent[key] = next_due

    def _fire(self, source: str) -> None:
        """Spawn a make_snapshot in a background thread under the op lock.
        If another op is already in flight, log it and skip -- the timer
        will try again on the next interval."""
        if not backups.try_begin_op(f"scheduled snapshot ({source})"):
            log.warning("scheduler: another backup op in progress -- "
                        "skipping this %s tick", source)
            return
        running = lifecycle.running_instances()
        if not running:
            # Race: state said running, but it ended between checks. Just
            # release and skip.
            log.warning("scheduler: race -- no running instances at fire time")
            backups._end_op()
            return
        log.info("scheduler: starting snapshot worker (source=%s, %d instance(s))",
                 source, len(running))
        threading.Thread(
            target=backups.run_snapshot_under_op,
            args=(running, source),
            daemon=True, name=f"snapshot-{source}",
        ).start()


# Module-level singleton so __main__ can start, and any caller can read
# state. start() is idempotent; safe to import multiple times.
_scheduler: Optional[BackupScheduler] = None


def start_scheduler() -> BackupScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackupScheduler()
    _scheduler.start()
    return _scheduler


def get_scheduler() -> Optional[BackupScheduler]:
    return _scheduler
