"""SQLite-backed static-world data store.

Holds the merged datamine of Points of Interest, icon assets, and
region-level metadata for the future /map page and Phase 4 spatial
enrichment ("died near Sand Dunes Dungeon in Barren Sandland").

Two distinct lifecycles share this DB:
  - poi / region tables: rarely written (loaded once at install,
    refreshed when datamine updates).
  - icon table: also rare-write blobs.

Compare to data/players.sqlite which is constantly written. Keeping
them separate so:
  - This DB ships read-only-ish to game-server VMs without churn.
  - We can rebuild this from source data without touching player
    history.
  - Different on-disk file sizes / backup strategies suit each.

Schema is intentionally minimal in v1; the milestone / achievement
system later may add tables.

Threading: same pattern as manager/players_db.py -- module
singleton connection (WAL mode, busy_timeout) and explicit lock.
Tests pass an explicit `path` to isolate.
"""

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from manager.config import DATA_DIR, PROJECT_ROOT

log = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "world.sqlite"
SCHEMA_VERSION = 2

# Known map levels. Both maps share the same UE coord system + tile-pixel
# transform constants, but POIs / regions are namespaced per level so a
# Sand Bandit in DLC and a different Sand Bandit in Cloudmist don't
# collide. The loader takes a level= parameter; queries can filter.
LEVEL_DLC = "DLC_Level01_Main"
LEVEL_BASE = "Level01_Main"
KNOWN_LEVELS = (LEVEL_DLC, LEVEL_BASE)

# Default paths for loader inputs.
#
# Datamine sources live in <PROJECT_ROOT>/tools/ and are gitignored
# (workstation-only). The operator regenerates world.sqlite when they
# refresh the datamine, then commits the updated DB so the server
# picks it up via Apply.
#
# Icon assets live in <PROJECT_ROOT>/data/icons/ and ARE tracked in
# git -- they ship with the manager so the server has them after
# Apply. (data/ is otherwise gitignored, but data/icons/ is allowlisted.)
DEFAULT_DATAMINE_PATH = PROJECT_ROOT / "tools" / "soulmask-DLC.json"
DEFAULT_ICONS_DIRS = (PROJECT_ROOT / "data" / "icons",)

# Verified UE-world -> in-game-map-grid transform (see
# memory/coordinate_transform.md). The DLC dump already carries
# pos.lat/lon for each item, so we don't need to compute -- but
# we expose the formula so other consumers can reuse it.
UE_TO_MAP_SCALE = 199.24
LON_OFFSET = 2047.91
LAT_OFFSET = -2048.47

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS poi (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    type TEXT NOT NULL,
    name TEXT,
    title TEXT,
    description TEXT,
    region TEXT,
    pos_x INTEGER NOT NULL,
    pos_y INTEGER NOT NULL,
    pos_z INTEGER,
    map_lat INTEGER,
    map_lon INTEGER,
    icon TEXT,
    saraserenity_key INTEGER,
    ellatha_id INTEGER
);
CREATE INDEX IF NOT EXISTS poi_by_level_type ON poi(level, type);
CREATE INDEX IF NOT EXISTS poi_by_level_region ON poi(level, region);
CREATE INDEX IF NOT EXISTS poi_by_pos ON poi(level, pos_x, pos_y);

CREATE TABLE IF NOT EXISTS icon (
    name TEXT PRIMARY KEY,
    blob BLOB,
    source TEXT,
    bytes INTEGER
);

CREATE TABLE IF NOT EXISTS region (
    level TEXT NOT NULL,
    name TEXT NOT NULL,
    poi_count INTEGER NOT NULL,
    bbox_min_x INTEGER,
    bbox_min_y INTEGER,
    bbox_max_x INTEGER,
    bbox_max_y INTEGER,
    centroid_x INTEGER,
    centroid_y INTEGER,
    PRIMARY KEY (level, name)
);
"""

# v1 -> v2 migration: add level column to poi (default 'DLC_Level01_Main'
# for existing rows), and recreate region with (level, name) composite PK.
_MIGRATE_V1_V2 = """
ALTER TABLE poi ADD COLUMN level TEXT NOT NULL DEFAULT 'DLC_Level01_Main';

CREATE INDEX IF NOT EXISTS poi_by_level_type ON poi(level, type);
CREATE INDEX IF NOT EXISTS poi_by_level_region ON poi(level, region);

DROP INDEX IF EXISTS poi_by_pos;
CREATE INDEX poi_by_pos ON poi(level, pos_x, pos_y);

DROP INDEX IF EXISTS poi_by_type;
DROP INDEX IF EXISTS poi_by_region;

CREATE TABLE region_v2 (
    level TEXT NOT NULL,
    name TEXT NOT NULL,
    poi_count INTEGER NOT NULL,
    bbox_min_x INTEGER, bbox_min_y INTEGER,
    bbox_max_x INTEGER, bbox_max_y INTEGER,
    centroid_x INTEGER, centroid_y INTEGER,
    PRIMARY KEY (level, name)
);
INSERT INTO region_v2 (level, name, poi_count,
                        bbox_min_x, bbox_min_y, bbox_max_x, bbox_max_y,
                        centroid_x, centroid_y)
    SELECT 'DLC_Level01_Main', name, poi_count,
           bbox_min_x, bbox_min_y, bbox_max_x, bbox_max_y,
           centroid_x, centroid_y
    FROM region;
DROP TABLE region;
ALTER TABLE region_v2 RENAME TO region;
"""


# ── Connection management ───────────────────────────────────────────────────


_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()


def open_db(path: Optional[Path] = None) -> sqlite3.Connection:
    """Open or reuse the singleton connection. Pass `path` for tests
    to isolate; production callers use the default."""
    if path is None:
        global _conn
        with _lock:
            if _conn is not None:
                return _conn
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = _connect_and_migrate(DB_PATH)
            return _conn
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return _connect_and_migrate(path)


def _connect_and_migrate(path: Path) -> sqlite3.Connection:
    log.info("world_db: opening %s", path)
    conn = sqlite3.connect(str(path), check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.execute("PRAGMA user_version").fetchone()[0]

    if cur < 1:
        log.info("world_db: fresh DB; applying schema v%d", SCHEMA_VERSION)
        conn.executescript(_SCHEMA_V2)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif cur < 2:
        # In-place v1 -> v2 migration. Existing POIs get tagged as
        # DLC_Level01_Main since that's the only map shipped before v2;
        # the operator can reload Cloudmist later to add more rows.
        log.info("world_db: migrating v%d -> v%d", cur, SCHEMA_VERSION)
        conn.executescript(_MIGRATE_V1_V2)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif cur > SCHEMA_VERSION:
        log.warning("world_db: on-disk schema version %d > code's %d",
                    cur, SCHEMA_VERSION)
    return conn


# ── Loader ─────────────────────────────────────────────────────────────────


def _coerce_int(value, default: int = 0) -> int:
    """Datamine sometimes has float coords like 81072.5; we round."""
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _load_pois(conn: sqlite3.Connection, datamine_path: Path,
                level: str) -> int:
    """Walk a datamine JSON and insert one poi row per item, all tagged
    with `level`. The loader's caller decides whether to wipe existing
    rows for this level first (load_world_data's replace_scope param)."""
    if not datamine_path.exists():
        log.warning("world_db loader: %s not found, skipping POI load "
                    "for level=%s", datamine_path, level)
        return 0
    log.info("world_db loader: reading %s (%.1f MB) for level=%s",
             datamine_path.name,
             datamine_path.stat().st_size / 1024 / 1024,
             level)
    with datamine_path.open(encoding="utf-8") as f:
        groups = json.load(f)
    if not isinstance(groups, list):
        log.error("world_db loader: %s top-level is %s, expected list",
                  datamine_path.name, type(groups).__name__)
        return 0

    rows = []
    for grp in groups:
        gtype = grp.get("type") or "(unknown)"
        gicon = grp.get("icon")
        for item in (grp.get("items") or []):
            data = item.get("data") or {}
            pos = item.get("pos") or {}
            try:
                rows.append((
                    level,
                    gtype,
                    data.get("name") or None,
                    data.get("title") or None,
                    data.get("desc") or None,
                    data.get("region") or None,
                    _coerce_int(data.get("posX")),
                    _coerce_int(data.get("posY")),
                    _coerce_int(data.get("posZ")) if data.get("posZ") is not None else None,
                    _coerce_int(pos.get("lat")) if pos.get("lat") is not None else None,
                    _coerce_int(pos.get("lon")) if pos.get("lon") is not None else None,
                    data.get("icon") or gicon or None,
                    item.get("key") if isinstance(item.get("key"), int) else None,
                    None,  # ellatha_id -- left NULL until we figure out match key
                ))
            except Exception:
                # One bad item shouldn't abort the whole load
                log.exception("world_db loader: skipping malformed item "
                              "in group %r", gtype)

    log.info("world_db loader: inserting %d POIs (level=%s)",
             len(rows), level)
    conn.executemany(
        "INSERT INTO poi(level, type, name, title, description, region, "
        "pos_x, pos_y, pos_z, map_lat, map_lon, icon, "
        "saraserenity_key, ellatha_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _load_icons(conn: sqlite3.Connection, icons_dirs) -> int:
    """Load icon blobs from each path in `icons_dirs`. By default that
    is just data/icons/ (saraserenity webps) since gamingwithdaopa
    PNGs are operator-side dev material in tools/alt-icons/.

    Each path is scanned for *.webp and *.png files. Existing rows
    are replaced (so a re-run with new icons updates them)."""
    rows = []
    for d in icons_dirs:
        d = Path(d)
        if not d.exists():
            continue
        for p in sorted(list(d.glob("*.webp")) + list(d.glob("*.png"))):
            try:
                blob = p.read_bytes()
                # Source label = the parent dir's name. Loader callers
                # pass per-source dirs so this is stable.
                source = d.name
                rows.append((p.stem, blob, source, len(blob)))
            except OSError as e:
                log.warning("world_db loader: failed to read %s: %s", p, e)

    if not rows:
        log.info("world_db loader: no icons found in any of %s",
                 [str(d) for d in icons_dirs])
        return 0

    log.info("world_db loader: inserting %d icons from %d source dir(s)",
             len(rows), len(icons_dirs))
    conn.executemany(
        "INSERT OR REPLACE INTO icon(name, blob, source, bytes) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _compute_regions(conn: sqlite3.Connection, level: str) -> int:
    """Derive region table from poi.region clusters for one level.
    Each region's bbox is the MBR of its POIs in UE coords; centroid
    is the simple average of pos_x/pos_y. Wipes existing region rows
    for this level before recomputing -- regions are derived data,
    not curated."""
    rows = conn.execute("""
        SELECT region,
               COUNT(*) AS n,
               MIN(pos_x) AS bbox_min_x, MIN(pos_y) AS bbox_min_y,
               MAX(pos_x) AS bbox_max_x, MAX(pos_y) AS bbox_max_y,
               ROUND(AVG(pos_x)) AS centroid_x,
               ROUND(AVG(pos_y)) AS centroid_y
        FROM poi
        WHERE level = ?
          AND region IS NOT NULL AND region != ''
        GROUP BY region
    """, (level,)).fetchall()
    payload = [(level, r["region"], r["n"],
                r["bbox_min_x"], r["bbox_min_y"],
                r["bbox_max_x"], r["bbox_max_y"],
                int(r["centroid_x"]), int(r["centroid_y"]))
               for r in rows]
    log.info("world_db loader: computed %d region(s) for level=%s",
             len(payload), level)
    conn.execute("DELETE FROM region WHERE level = ?", (level,))
    conn.executemany(
        "INSERT INTO region(level, name, poi_count, "
        "bbox_min_x, bbox_min_y, bbox_max_x, bbox_max_y, "
        "centroid_x, centroid_y) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        payload,
    )
    return len(payload)


# Default datamine paths per known level. The loader picks the right
# one based on its `level` argument.
DEFAULT_DATAMINE_PATHS_PER_LEVEL = {
    LEVEL_DLC:  PROJECT_ROOT / "tools" / "soulmask-DLC.json",
    LEVEL_BASE: PROJECT_ROOT / "tools" / "soulmask-Cloudmist.json",
}


def load_world_data(level: str = LEVEL_DLC,
                    datamine_path: Optional[Path] = None,
                    icons_dirs: Optional[list] = None,
                    db: Optional[sqlite3.Connection] = None,
                    *,
                    replace_scope: str = "this_level") -> dict:
    """Ingest the static datamine for one map into the DB.

    `level` selects which map's data to load. Existing rows for THAT
    level (and only that level) are replaced by default, so loading
    Cloudmist doesn't wipe DLC and vice versa.

    Defaults:
      - level: 'DLC_Level01_Main'
      - datamine_path: tools/soulmask-DLC.json (level-specific default
        per DEFAULT_DATAMINE_PATHS_PER_LEVEL). Override for tests.
      - icons_dirs: (data/icons/,) -- shared across levels (icons
        live in one namespace).

    `replace_scope`:
      - 'this_level' (default) -- wipe POIs+regions for `level` only;
        other levels untouched.
      - 'all' -- wipe all POIs+regions before insert. Use for a clean
        single-level rebuild from scratch.
      - 'none' -- append only; no wipe. Reserved for incremental ops.

    Returns {'pois': N, 'icons': N, 'regions': N, 'level': str}.

    Raises FileNotFoundError if the datamine_path doesn't exist (the
    operator hasn't downloaded that level's datamine yet)."""
    if level not in KNOWN_LEVELS:
        log.warning("world_db loader: unknown level %r (KNOWN_LEVELS=%s) "
                    "-- proceeding anyway", level, KNOWN_LEVELS)
    if datamine_path is None:
        datamine_path = DEFAULT_DATAMINE_PATHS_PER_LEVEL.get(
            level, PROJECT_ROOT / "tools" / "soulmask-DLC.json"
        )
    datamine_path = Path(datamine_path)

    if icons_dirs is None:
        icons_dirs = list(DEFAULT_ICONS_DIRS)
    icons_dirs = [Path(d) for d in icons_dirs]

    if not datamine_path.exists():
        raise FileNotFoundError(
            f"datamine for level={level!r} not on disk: {datamine_path}\n"
            f"Run `python tools/scraper.py pull` to fetch it, or pass an "
            f"explicit datamine_path= override."
        )

    conn = db or open_db()
    use_lock = _lock if db is None else _NoopLock()
    with use_lock:
        if replace_scope == "all":
            conn.execute("DELETE FROM poi")
            conn.execute("DELETE FROM region")
            log.info("world_db loader: cleared ALL poi+region rows "
                     "(replace_scope=all)")
        elif replace_scope == "this_level":
            conn.execute("DELETE FROM poi WHERE level = ?", (level,))
            conn.execute("DELETE FROM region WHERE level = ?", (level,))
            log.info("world_db loader: cleared poi+region rows for "
                     "level=%s", level)
        # replace_scope == 'none' -- skip the delete

        n_poi = _load_pois(conn, datamine_path, level)
        n_icon = _load_icons(conn, icons_dirs)   # icons are level-shared
        n_region = _compute_regions(conn, level)

    summary = {"pois": n_poi, "icons": n_icon,
               "regions": n_region, "level": level}
    log.info("world_db loader: done -- %s", summary)
    return summary


# ── Query helpers (Phase 4 / map page will build on these) ─────────────────


def get_poi_count(level: Optional[str] = None,
                   db: Optional[sqlite3.Connection] = None) -> int:
    """Total POI count, or POIs in a single level when `level` set."""
    conn = db or open_db()
    if level is None:
        return int(conn.execute(
            "SELECT COUNT(*) AS c FROM poi").fetchone()["c"])
    return int(conn.execute(
        "SELECT COUNT(*) AS c FROM poi WHERE level=?", (level,)
    ).fetchone()["c"])


def find_pois_by_type(type_: str,
                       level: Optional[str] = None,
                       db: Optional[sqlite3.Connection] = None) -> list:
    """All POIs of a given type. Pass `level` to scope to one map; or
    None to get matches from all levels."""
    conn = db or open_db()
    if level is None:
        cur = conn.execute("SELECT * FROM poi WHERE type=?", (type_,))
    else:
        cur = conn.execute(
            "SELECT * FROM poi WHERE type=? AND level=?",
            (type_, level))
    return [dict(r) for r in cur.fetchall()]


def find_region_for(pos_x: int, pos_y: int,
                    level: Optional[str] = None,
                    db: Optional[sqlite3.Connection] = None
                    ) -> Optional[dict]:
    """Return the region whose MBR contains (pos_x, pos_y), or None.
    `level` should normally be supplied -- both maps' regions live in
    the same table, and the same UE coord COULD fall inside bboxes
    from both maps if their world-extents overlap. Without `level`,
    we return whichever has the smallest (most-specific) bbox.

    Many regions overlap within a single map (the world's nested),
    so on multi-match we return the smallest area as the most
    specific."""
    conn = db or open_db()
    if level is None:
        cur = conn.execute("""
            SELECT *,
                   (bbox_max_x - bbox_min_x) * (bbox_max_y - bbox_min_y) AS area
            FROM region
            WHERE bbox_min_x <= ? AND ? <= bbox_max_x
              AND bbox_min_y <= ? AND ? <= bbox_max_y
            ORDER BY area ASC
            LIMIT 1
        """, (pos_x, pos_x, pos_y, pos_y))
    else:
        cur = conn.execute("""
            SELECT *,
                   (bbox_max_x - bbox_min_x) * (bbox_max_y - bbox_min_y) AS area
            FROM region
            WHERE level = ?
              AND bbox_min_x <= ? AND ? <= bbox_max_x
              AND bbox_min_y <= ? AND ? <= bbox_max_y
            ORDER BY area ASC
            LIMIT 1
        """, (level, pos_x, pos_x, pos_y, pos_y))
    rows = cur.fetchall()
    return dict(rows[0]) if rows else None


def nearest_poi(pos_x: int, pos_y: int, *, max_results: int = 1,
                type_filter: Optional[str] = None,
                level: Optional[str] = None,
                db: Optional[sqlite3.Connection] = None) -> list:
    """Brute-force nearest-neighbour search by squared-distance.
    With ~10k rows in poi (per level), a linear scan is fine -- no
    spatial index needed.

    `level` filter is normally REQUIRED for sensible results (POIs
    from different maps may have similar UE coords). Default None
    keeps all-level search for testing / debug; production callers
    should pass the player's current map level explicitly."""
    conn = db or open_db()
    sql_parts = [
        "SELECT *, "
        "((pos_x - ?) * (pos_x - ?) + (pos_y - ?) * (pos_y - ?)) AS d2 "
        "FROM poi WHERE 1=1"
    ]
    args: list = [pos_x, pos_x, pos_y, pos_y]
    if type_filter:
        sql_parts.append("AND type = ?")
        args.append(type_filter)
    if level:
        sql_parts.append("AND level = ?")
        args.append(level)
    sql_parts.append("ORDER BY d2 ASC LIMIT ?")
    args.append(max_results)
    cur = conn.execute(" ".join(sql_parts), args)
    return [dict(r) for r in cur.fetchall()]


def get_icon(name: str,
             db: Optional[sqlite3.Connection] = None) -> Optional[bytes]:
    """Return raw icon bytes or None if missing."""
    conn = db or open_db()
    row = conn.execute("SELECT blob FROM icon WHERE name=?", (name,)).fetchone()
    return bytes(row["blob"]) if row and row["blob"] else None


# ── Helpers ─────────────────────────────────────────────────────────────────


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
