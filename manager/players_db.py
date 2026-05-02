"""SQLite-backed player tracking.

Stores player presence (sessions), per-event journal, and rollup
counters that the future milestone / achievement system will read.

Schema is intentionally minimal in v1:

    player          one row per known steam_id (display name + first/last seen)
    session         one row per join->leave pair (NULL left_at while open)
    event           one row per relevant GameEvent (journal -- LLM phase reads this)
    stat            (steam_id, key) -> integer counter (rollups / milestones read this)

`event` is the firehose journal: every relayable event tied to its
player's open session if applicable. `stat` is the small derived
rollup ("elite_kills", "deaths", "bandages_used"). The split lets
the milestone / LLM-summary phases query whichever shape fits --
journal reads for LLM context, stat reads for milestone thresholds.

Threading:
- A module-level singleton connection is opened with
  check_same_thread=False and guarded by a Lock.
- WAL mode + busy_timeout configured at open time for concurrent
  read+write tolerance. Write volume is low (a few writes per
  player action; tens per minute on a busy server) so a single
  connection is fine.
- Public functions accept an optional `db` argument so tests can
  pass an isolated tempdir connection without touching the live
  data/players.sqlite.
"""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from manager.config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "players.sqlite"
SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS player (
    steam_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    steam_id TEXT NOT NULL,
    joined_at REAL NOT NULL,
    left_at REAL,
    duration_sec REAL,
    FOREIGN KEY (steam_id) REFERENCES player(steam_id)
);
CREATE INDEX IF NOT EXISTS ix_session_steam_id ON session(steam_id);
CREATE INDEX IF NOT EXISTS ix_session_open
    ON session(steam_id) WHERE left_at IS NULL;

CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES session(id)
);
CREATE INDEX IF NOT EXISTS ix_event_session ON event(session_id);
CREATE INDEX IF NOT EXISTS ix_event_at ON event(at);

CREATE TABLE IF NOT EXISTS stat (
    steam_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (steam_id, key),
    FOREIGN KEY (steam_id) REFERENCES player(steam_id)
);
"""


# ── Connection management ───────────────────────────────────────────────────


_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()


def open_db(path: Optional[Path] = None) -> sqlite3.Connection:
    """Open or reuse the singleton DB connection. `path=None` uses
    the production DATA_DIR/players.sqlite. Tests pass an explicit
    path to isolate.

    Idempotent: subsequent calls with the same default path return
    the same connection. Tests calling with an explicit path always
    get a fresh connection (so cleanup between scenarios works)."""
    if path is None:
        global _conn
        with _lock:
            if _conn is not None:
                return _conn
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = _connect_and_migrate(DB_PATH)
            return _conn
    # Explicit path -> fresh connection, not cached.
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return _connect_and_migrate(path)


def _connect_and_migrate(path: Path) -> sqlite3.Connection:
    log.info("players_db: opening %s", path)
    conn = sqlite3.connect(str(path), check_same_thread=False,
                           isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")

    cur_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if cur_version < 1:
        log.info("players_db: applying schema v1")
        conn.executescript(_SCHEMA_V1)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif cur_version > SCHEMA_VERSION:
        log.warning("players_db: schema version %d on disk is newer than "
                    "this code's version %d -- proceeding read-only-ish; "
                    "operator may have downgraded the manager",
                    cur_version, SCHEMA_VERSION)
    return conn


# ── Player + session API ────────────────────────────────────────────────────


def record_join(steam_id: str, display_name: str,
                ts: Optional[float] = None,
                db: Optional[sqlite3.Connection] = None) -> int:
    """Mark a player as joined. Upserts player row and opens a new
    session. Returns the new session id.

    If a session for this player is already open (we missed a leave
    event due to a server crash, etc.), close it with a synthetic
    `left_at = ts` first so we don't accumulate phantom open
    sessions."""
    if ts is None:
        ts = time.time()
    conn = db or open_db()
    with _lock if db is None else _NoopLock():
        # Upsert player.
        conn.execute(
            "INSERT INTO player(steam_id, display_name, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(steam_id) DO UPDATE SET "
            "display_name=excluded.display_name, last_seen=excluded.last_seen",
            (steam_id, display_name, ts, ts),
        )
        # Force-close any stale open session for this player.
        conn.execute(
            "UPDATE session SET left_at=?, duration_sec=?-joined_at "
            "WHERE steam_id=? AND left_at IS NULL",
            (ts, ts, steam_id),
        )
        # Open new session.
        cur = conn.execute(
            "INSERT INTO session(steam_id, joined_at) VALUES (?, ?)",
            (steam_id, ts),
        )
        return int(cur.lastrowid)


def record_leave(steam_id: str,
                 ts: Optional[float] = None,
                 db: Optional[sqlite3.Connection] = None
                 ) -> Optional[int]:
    """Close the most recent open session for `steam_id`. Returns
    the closed session id, or None if no open session was found."""
    if ts is None:
        ts = time.time()
    conn = db or open_db()
    with _lock if db is None else _NoopLock():
        row = conn.execute(
            "SELECT id, joined_at FROM session "
            "WHERE steam_id=? AND left_at IS NULL "
            "ORDER BY joined_at DESC LIMIT 1",
            (steam_id,),
        ).fetchone()
        if row is None:
            return None
        sid = int(row["id"])
        duration = ts - float(row["joined_at"])
        conn.execute(
            "UPDATE session SET left_at=?, duration_sec=? WHERE id=?",
            (ts, duration, sid),
        )
        conn.execute(
            "UPDATE player SET last_seen=? WHERE steam_id=?",
            (ts, steam_id),
        )
        return sid


def open_session_id(steam_id: str,
                    db: Optional[sqlite3.Connection] = None
                    ) -> Optional[int]:
    """Return the id of the currently-open session for steam_id,
    or None if there isn't one."""
    conn = db or open_db()
    row = conn.execute(
        "SELECT id FROM session WHERE steam_id=? AND left_at IS NULL "
        "ORDER BY joined_at DESC LIMIT 1",
        (steam_id,),
    ).fetchone()
    return int(row["id"]) if row else None


# ── Event journal ───────────────────────────────────────────────────────────


def append_event(kind: str, payload: dict, ts: Optional[float] = None,
                 session_id: Optional[int] = None,
                 db: Optional[sqlite3.Connection] = None) -> int:
    """Append a row to the event journal. payload must be JSON-
    serializable. Returns the new event id."""
    if ts is None:
        ts = time.time()
    conn = db or open_db()
    body = json.dumps(payload, default=str)
    with _lock if db is None else _NoopLock():
        cur = conn.execute(
            "INSERT INTO event(session_id, at, kind, payload_json) "
            "VALUES (?, ?, ?, ?)",
            (session_id, ts, kind, body),
        )
        return int(cur.lastrowid)


# ── Stat counters ───────────────────────────────────────────────────────────


def bump_stat(steam_id: str, key: str, delta: int = 1,
              db: Optional[sqlite3.Connection] = None) -> int:
    """Increment a counter, creating the row if needed. Returns the
    new value."""
    conn = db or open_db()
    with _lock if db is None else _NoopLock():
        conn.execute(
            "INSERT INTO stat(steam_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(steam_id, key) DO UPDATE SET value=value+?",
            (steam_id, key, delta, delta),
        )
        row = conn.execute(
            "SELECT value FROM stat WHERE steam_id=? AND key=?",
            (steam_id, key),
        ).fetchone()
        return int(row["value"]) if row else 0


def get_stat(steam_id: str, key: str, default: int = 0,
             db: Optional[sqlite3.Connection] = None) -> int:
    conn = db or open_db()
    row = conn.execute(
        "SELECT value FROM stat WHERE steam_id=? AND key=?",
        (steam_id, key),
    ).fetchone()
    return int(row["value"]) if row else default


def last_known_location(steam_id: str,
                         db: Optional[sqlite3.Connection] = None
                         ) -> Optional[dict]:
    """Find the most recent event for this player whose payload carries
    a location. Returns {'x', 'y', 'z', 'at', 'kind'} or None if no
    such event exists yet (first-time player or no events with coords).

    Used by Discord image-on-join: we don't have player coords AT the
    join moment, but the location they last appeared in events is a
    decent proxy (typically where they logged out, which is near where
    they spawn back in)."""
    conn = db or open_db()
    # Walk up to ~50 most recent events for this player. Most events
    # carry a location; we stop on the first one that does.
    rows = conn.execute(
        "SELECT e.at, e.kind, e.payload_json "
        "FROM event e "
        "JOIN session s ON e.session_id = s.id "
        "WHERE s.steam_id = ? "
        "ORDER BY e.at DESC LIMIT 50",
        (steam_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        loc = payload.get("location")
        if loc and isinstance(loc, dict) and "x" in loc and "y" in loc:
            return {
                "x": float(loc["x"]),
                "y": float(loc["y"]),
                "z": float(loc.get("z", 0)) if loc.get("z") is not None else None,
                "at": float(row["at"]),
                "kind": row["kind"],
            }
    return None


def display_name(steam_id: str,
                 db: Optional[sqlite3.Connection] = None
                 ) -> Optional[str]:
    """Return the most recently recorded display name for `steam_id`,
    or None if this player has never been seen. Survives manager
    restarts (in-memory session maps in events.py do not), so this is
    the right fallback when a leave event arrives for a player whose
    join scrolled out of the tailer history buffer."""
    conn = db or open_db()
    row = conn.execute(
        "SELECT display_name FROM player WHERE steam_id = ?",
        (steam_id,),
    ).fetchone()
    if row is None:
        return None
    name = row["display_name"]
    return name if name else None


# ── Helpers ─────────────────────────────────────────────────────────────────


class _NoopLock:
    """Used instead of the module lock when a test passes its own
    connection -- the test owns the connection's serialisation."""
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
