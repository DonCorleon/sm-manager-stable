"""Shared activity log for the /updates page.

Both the manager (git) self-update path and the Steam (SteamCMD) update
path stream their subprocess output line-by-line into a single bounded
deque here. The /updates page subscribes via SSE and shows a unified
terminal-style view of "what's happening with updates right now" --
covers click-Check-for-updates, click-Apply, click-Test-connection,
click-Force-sync, and the Steam build-id check.

(See also manager/self_update.py:compile_check_ref -- as of 2026-04-30
that function does an in-memory syntax check via tarfile + compile()
instead of extractall + compileall, avoiding a 60-90s Windows
Defender stall that was blocking every Apply on the server.)

Design mirrors player_tracker / EventStream:

  - Bounded deque (drop oldest) so a noisy command can't OOM the manager.
  - One `threading.Event` per subscriber. Producers set them all on each
    append; subscribers do `wait() -> drain -> clear()`.
  - Op-state struct ('idle' / 'running:<name>' / 'done' / 'failed')
    surfaced via `get_op_state()` so the SSE consumer can render
    "Apply in progress..." / "Apply finished (ok)" headlines.

Producers are call-from-anywhere safe: every public function takes the
locks it needs and never calls back into producer code while holding them.
"""

import logging
import threading
import time
from collections import deque
from typing import Optional

log = logging.getLogger(__name__)

# Drop-oldest cap. ~2k lines covers a long Apply (git fetch + py_compile
# + git pull is a few hundred lines) without unbounded growth.
_MAX_LINES = 2000

# Each entry: (ts_unix_seconds, source, line). source is "manager" or
# "steam" (UI uses it for color coding); free-form is OK for future use.
_lines: deque = deque(maxlen=_MAX_LINES)
_lines_lock = threading.Lock()
_seq = 0  # monotonic id so SSE clients can resume from a known offset

_subscribers: list[threading.Event] = []
_subs_lock = threading.Lock()

# Op-state. Single global because only one user-triggered update op at a
# time makes sense -- they all touch the same git/SteamCMD state.
_op_lock = threading.Lock()
_op_state: dict = {
    "phase": "idle",        # idle / running / done / failed
    "name": "",             # human label, e.g. "Apply manager update"
    "source": "",           # "manager" / "steam"
    "started_at": 0.0,      # unix ts
    "finished_at": 0.0,
    "error": "",
}


def append(source: str, line: str) -> None:
    """Append one line to the log and wake all subscribers. Trims any
    trailing CR/LF; empty lines are dropped silently."""
    line = line.rstrip("\r\n")
    if not line:
        return
    global _seq
    ts = time.time()
    with _lines_lock:
        _seq += 1
        _lines.append((_seq, ts, source, line))
    # Notify under its own lock so a slow producer never blocks here.
    with _subs_lock:
        subs = list(_subscribers)
    for ev in subs:
        ev.set()


def snapshot(since_seq: int = 0, max_count: int = 500) -> list[tuple]:
    """Return up to `max_count` entries with seq > since_seq, oldest first.
    Used by the SSE endpoint to backfill new clients with recent context."""
    with _lines_lock:
        # deque slicing is O(n); we want the tail. Walk backwards.
        out: list[tuple] = []
        for entry in reversed(_lines):
            if entry[0] <= since_seq:
                break
            out.append(entry)
            if len(out) >= max_count:
                break
        out.reverse()
        return out


def latest_seq() -> int:
    with _lines_lock:
        return _seq


def subscribe() -> threading.Event:
    """Return a fresh Event object that fires whenever a new line is
    appended. Caller MUST call unsubscribe() when done so the producer
    side doesn't keep setting a leaked Event forever."""
    ev = threading.Event()
    with _subs_lock:
        _subscribers.append(ev)
    return ev


def unsubscribe(ev: threading.Event) -> None:
    with _subs_lock:
        try:
            _subscribers.remove(ev)
        except ValueError:
            pass


def subscriber_count() -> int:
    with _subs_lock:
        return len(_subscribers)


# ── Op-state ───────────────────────────────────────────────────────────────


def begin_op(name: str, source: str) -> bool:
    """Try to claim the op slot. Returns False if another op is already
    running -- caller should refuse the user click in that case.

    Writes a banner line to the log so the SSE consumer sees the
    transition without polling get_op_state."""
    with _op_lock:
        if _op_state["phase"] == "running":
            log.warning("updates_log: refusing %r -- %r still running",
                        name, _op_state["name"])
            return False
        _op_state["phase"] = "running"
        _op_state["name"] = name
        _op_state["source"] = source
        _op_state["started_at"] = time.time()
        _op_state["finished_at"] = 0.0
        _op_state["error"] = ""
    append(source, f"========== {name} ==========")
    return True


def end_op(ok: bool, error: str = "") -> None:
    """Mark the current op as finished. Appends a banner line."""
    with _op_lock:
        _op_state["phase"] = "done" if ok else "failed"
        _op_state["finished_at"] = time.time()
        _op_state["error"] = error or ""
        name = _op_state["name"]
        source = _op_state["source"]
    if ok:
        append(source, f"---------- {name} : OK ----------")
    else:
        append(source, f"---------- {name} : FAILED : {error or '(no detail)'} ----------")


def get_op_state() -> dict:
    """Snapshot of the current/last op for the page header. Always returns
    a fresh dict (safe to mutate by the caller)."""
    with _op_lock:
        return dict(_op_state)
