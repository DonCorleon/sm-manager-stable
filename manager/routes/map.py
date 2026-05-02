"""/map/* routes -- the world-map page + its supporting endpoints.

Layout:
  /map/                                 main page (Leaflet-driven)
  /map/tiles/<level>/<z>/<x>/<y>.png    static tile pyramid
  /map/api/types/<level>                POI category counts (sidebar)
  /map/api/pois/<level>?type=<T>        POI markers (per-category fetch)
  /map/api/icon/<name>                  per-POI icon BLOB from world.sqlite

Tiles ship in git at data/map/<level>/<z>/<x>/<y>.png so the server
has them after `git pull`. The endpoints serve them with sane cache
headers and reject obviously-malicious paths.
"""

import io
import json
import logging
from pathlib import Path

from flask import Blueprint, Response, abort, jsonify, render_template, request, send_file

from manager.config import PROJECT_ROOT
from manager.world_db import KNOWN_LEVELS

map_bp = Blueprint("map", __name__, url_prefix="/map")
log = logging.getLogger(__name__)

# Tile pyramid covers zoom 1..6 with 2^z x 2^z tiles. Reject anything
# outside this range hard so a malicious URL can't traverse off the
# tracked tile area.
_TILE_ZOOM_RANGE = (1, 6)
_TILE_ROOT = PROJECT_ROOT / "data" / "map"
# Cache aggressively -- tiles only change when the operator regenerates
# the world (re-mining a new datamine). Browser revalidates on hard
# refresh. 1 day is generous and avoids stale-after-an-hour annoyances.
_TILE_CACHE_SECONDS = 86400


@map_bp.route("/")
def index():
    """Main map page. Renders the Leaflet shell; data is fetched
    asynchronously by the JS in the template."""
    log.debug("map.index render")
    # Pass the known levels + which one is the operator's "main" so
    # the level switcher can default sensibly.
    from manager.paths import level_for_server
    main_level = level_for_server("ws") or KNOWN_LEVELS[0]
    return render_template(
        "map.html",
        levels=list(KNOWN_LEVELS),
        main_level=main_level,
        level_labels=_level_labels(),
    )


def _level_labels() -> dict:
    """Resolve display name for each known level.

    Resolution order, per operator preference:
      1. Configured `instance.short_name` whose `map_name` matches the
         level. Respects whatever the operator named the instance in
         the wizard / settings.
      2. MAP_DEFAULTS[level]['name'] -- the spaceless default
         ("CloudMist", "ShiftingSands").
      3. The raw level identifier as last-resort fallback.

    Cached per-render only -- if the operator renames an instance and
    refreshes the page, the new name shows up on the next render."""
    out = {}
    try:
        from manager.config import load_settings
        from manager.wizard import MAP_DEFAULTS, load_existing
        config = load_existing(load_settings())
    except Exception:
        config = None
        MAP_DEFAULTS = {}

    for level in KNOWN_LEVELS:
        label = None
        if config and getattr(config, "instances", None):
            for inst in config.instances:
                if getattr(inst, "map_name", None) == level:
                    label = getattr(inst, "short_name", None) or \
                            getattr(inst, "name", None)
                    if label:
                        break
        if not label and level in MAP_DEFAULTS:
            label = MAP_DEFAULTS[level].get("name")
        out[level] = label or level
    return out


@map_bp.route("/tiles/<level>/<int:z>/<int:x>/<int:y>.png")
def tile(level: str, z: int, x: int, y: int):
    """Serve one PNG tile from data/map/<level>/<z>/<x>/<y>.png.

    Validates everything before touching the filesystem so a
    crafted URL can never path-traverse out of the tracked area:
      - level must be one of KNOWN_LEVELS
      - z must be 1..6
      - x, y must each be 0..(2^z - 1)
    Returns 404 if any check fails or the file is missing."""
    if level not in KNOWN_LEVELS:
        log.debug("map.tile: rejected unknown level %r", level)
        abort(404)
    z_min, z_max = _TILE_ZOOM_RANGE
    if not (z_min <= z <= z_max):
        log.debug("map.tile: rejected zoom %d (range %d..%d)",
                  z, z_min, z_max)
        abort(404)
    n = 1 << z  # 2^z
    if not (0 <= x < n and 0 <= y < n):
        log.debug("map.tile: rejected (x=%d, y=%d) for zoom %d "
                  "(grid is %dx%d)", x, y, z, n, n)
        abort(404)

    path = _TILE_ROOT / level / str(z) / str(x) / f"{y}.png"
    if not path.is_file():
        # Common when a level's tile pyramid hasn't been mined yet.
        # No log spam -- happens once per missing tile per page load.
        return "", 404

    response = send_file(path, mimetype="image/png", max_age=_TILE_CACHE_SECONDS)
    # Tiles are immutable for the lifetime of a deploy. mark them
    # public so any intermediate cache (CDN, proxy) can store them.
    response.headers["Cache-Control"] = (
        f"public, max-age={_TILE_CACHE_SECONDS}, immutable"
    )
    return response


# ── API endpoints (frontend XHR / fetch consumers) ─────────────────────────


# ── POI classifier ─────────────────────────────────────────────────────────
#
# ~145 positional types per level is too many for a flat sidebar. We
# bucket them into 11 high-level categories (operator-approved). A
# "type" lands in the FIRST category whose rule matches; ordering of
# CATEGORY_RULES therefore matters.
#
# Rules use simple substring / suffix / prefix match. When a new POI
# type appears (datamine refresh) it lands in "Other" and the operator
# can ask for a rule update; nothing breaks.

_CATEGORY_ORDER = [
    "Tribes & bandits",
    "Bosses",
    "Animals",
    "Eggs",
    "Mining & resources",
    "Loot containers",
    "Ruins & dungeons",
    "Relics & treasures",
    "Lore & mystery",
    "NPCs & utility",
    "Other",
]

# Each rule: (category, predicate). predicate(type_name) -> bool.
# Order matters: first match wins.


def _classify_poi_type(t: str) -> str:
    """Return the category bucket for a POI type name.
    See _CATEGORY_ORDER for the buckets and their intent."""
    s = t.strip()
    sl = s.lower()

    # Bosses: check before Tribes, since "Plunderer King of the Region"
    # would otherwise land in Tribes.
    if s.startswith("Elite "):
        return "Bosses"
    if "World Boss" in s or s == "Boss Altar":
        return "Bosses"
    if "King" in s and ("Rat" in s or "Plunderer" in s):
        return "Bosses"

    # NPC carve-outs that contain "Tribe" must match BEFORE the broad
    # tribe rule below; otherwise "Tribe Merchant" lands in Tribes
    # rather than NPCs where the operator-approved bucketing puts it.
    if s in ("Tribe Merchant", "Tribe Transport Boat"):
        return "NPCs & utility"

    # Tribes & bandits.
    if any(tok in s for tok in (
            "Tribe", "Bandit", "Plunderer", "Barbarian", "Invader",
            "Claw Tribe", "Fang Tribe", "Flint Tribe",
            "Wildwolf Tribe", "Savagehorn Tribe")):
        return "Tribes & bandits"

    # Eggs.
    if sl.endswith(" egg"):
        return "Eggs"

    # Mining & resources.
    if (s.endswith(" Vein") or s == "Mine"
            or s.startswith("Mine (") or s == "Mining Platform"
            or s == "Salt Mine"):
        return "Mining & resources"

    # Loot containers.
    if ("Storage Box" in s or "Supplies Chest" in s
            or "Casket" in s):
        return "Loot containers"

    # Ruins & dungeons.
    if (s.startswith("Ruins") or s.startswith("Dungeon")
            or "Pyramid" in s or s == "Cave"
            or s == "Beast Lair" or s == "Arena"
            or "Ancient Dungeon" in s):
        return "Ruins & dungeons"

    # Relics & treasures.
    if "Relic" in s or "Golden Legend" in s:
        return "Relics & treasures"

    # Lore & mystery.
    if (s.startswith("Tablet")
            or s.startswith("Mysterious")
            or s.startswith("Portal Part")
            or "Anti-Radiation" in s
            or s.startswith("Anti-gravity")
            or "hibernation pod" in sl
            or s == "Mechanical"):
        return "Lore & mystery"

    # NPCs & utility.
    if s in ("Merchant", "Tribe Merchant", "Respawn Point",
             "Boat", "Tribe Transport Boat"):
        return "NPCs & utility"

    # Anything else: fauna fallback (Boar, Wolf, etc.) -> Animals,
    # except a few non-creature stragglers go to Other.
    if s in ("(Multiple)", "Unaffiliated"):
        return "Other"
    return "Animals"


@map_bp.route("/api/types/<level>")
def api_types(level: str):
    """Return POI category counts for a level, grouped into operator-
    facing buckets.

    Response shape:
      {"categories": [
         {"name": "Tribes & bandits",
          "types": [{"type": "Sand Bandit", "count": 830, "icon": "..."},
                    ...]
         },
         ...
      ]}

    Filters: types whose entries are 100% (0,0) summary rows are
    omitted -- those rows are datamine aggregates ("Iron Ore: 13
    deposits") with no real position. Mixed types keep their
    positional count and appear in the sidebar; the actual (0,0)
    rows are filtered server-side in /api/pois.

    Within each category, types are alphabetical."""
    if level not in KNOWN_LEVELS:
        abort(404)
    from manager import world_db
    db = world_db.open_db()
    rows = db.execute(
        "SELECT type, "
        "       COUNT(*) AS total, "
        "       SUM(CASE WHEN pos_x=0 AND pos_y=0 THEN 0 ELSE 1 END) "
        "         AS positional, "
        "       MIN(icon) AS icon "
        "FROM poi WHERE level=? "
        "GROUP BY type "
        "HAVING positional > 0 "
        "ORDER BY type",
        (level,),
    ).fetchall()

    buckets: dict[str, list] = {c: [] for c in _CATEGORY_ORDER}
    for r in rows:
        cat = _classify_poi_type(r["type"])
        buckets[cat].append({
            "type": r["type"],
            "count": r["positional"],
            "icon": r["icon"],
        })

    out = []
    for cat in _CATEGORY_ORDER:
        types = buckets[cat]
        if not types:
            continue
        # Already sorted by ORDER BY type; double-belt to be explicit.
        types.sort(key=lambda t: t["type"].lower())
        out.append({"name": cat, "types": types})

    response = jsonify({"categories": out})
    response.headers["Cache-Control"] = "no-store"
    return response


@map_bp.route("/api/pois/<level>")
def api_pois(level: str):
    """Return POIs for a level. Optional `?type=<X>` filter for
    per-category lazy-loading. Returns small dicts the frontend can
    plot directly:
      [{x, y, type, name, title, region, icon}, ...]"""
    if level not in KNOWN_LEVELS:
        abort(404)
    type_filter = request.args.get("type")
    from manager import world_db
    db = world_db.open_db()
    # Exclude (0,0) summary rows. Those are datamine aggregates with
    # no real position (e.g. "Iron Ore: 13 deposits"); rendering them
    # produces a misleading dot at world centre.
    if type_filter:
        rows = db.execute(
            "SELECT type, name, title, region, pos_x, pos_y, icon "
            "FROM poi WHERE level=? AND type=? "
            "AND NOT (pos_x=0 AND pos_y=0) "
            "ORDER BY name",
            (level, type_filter),
        ).fetchall()
    else:
        # Without filter, return all POIs for the level. Larger
        # payload (10-14k rows ~= 2-3 MB JSON) but lets the frontend
        # do filtering client-side if it prefers.
        rows = db.execute(
            "SELECT type, name, title, region, pos_x, pos_y, icon "
            "FROM poi WHERE level=? "
            "AND NOT (pos_x=0 AND pos_y=0) "
            "ORDER BY type, name",
            (level,),
        ).fetchall()
    out = [{
        "x": r["pos_x"],
        "y": r["pos_y"],
        "type": r["type"],
        "name": r["name"],
        "title": r["title"],
        "region": r["region"],
        "icon": r["icon"],
    } for r in rows]
    response = jsonify({"level": level, "count": len(out), "pois": out})
    response.headers["Cache-Control"] = "no-store"
    return response


@map_bp.route("/api/icon/<name>")
def api_icon(name: str):
    """Serve an icon BLOB from world.sqlite. Used by markers on the
    /map page (Leaflet's L.icon expects a URL). Cached aggressively
    since icons only change when the operator regenerates the DB."""
    # Reject obviously-malicious names (no path separators, no '..')
    if "/" in name or "\\" in name or ".." in name:
        abort(404)
    from manager import world_db
    blob = world_db.get_icon(name)
    if not blob:
        abort(404)
    return send_file(
        io.BytesIO(blob),
        mimetype="image/webp",
        download_name=f"{name}.webp",
        max_age=_TILE_CACHE_SECONDS,
    )


# ── SSE: live online-player positions ─────────────────────────────────────


# Initial-burst padding to bust through Werkzeug response buffer
# (same trick used by /logs/sse). Without it some clients sit in
# "connecting" state until enough bytes accumulate.
_SSE_INITIAL_PADDING = (":" + (" " * 2048) + "\n\n").encode("utf-8")
_SSE_RETRY_FRAME = b"retry: 5000\n\n"
_SSE_KEEPALIVE = b": keepalive\n\n"
_SSE_KEEPALIVE_INTERVAL_SEC = 25.0


def _sse_players_frame(state: dict) -> bytes:
    """Encode a player-state snapshot as a named SSE event."""
    payload = json.dumps(state, ensure_ascii=False)
    return f"event: players\ndata: {payload}\n\n".encode("utf-8")


@map_bp.route("/api/players/stream")
def api_players_stream():
    """SSE stream of live online-player positions.

    Frame format: event named 'players', data is a
    {steam_id: {name, x, y, z, level, last_seen}} dict.

    Frequency: pushed on each state change (typically every 30 sec
    while at least one running instance has online players, less
    often when nothing is happening). Keepalive comments every 25
    sec keep the connection from being torn down by intermediaries.

    The PlayerTracker singleton was started at boot in
    manager/__main__.py."""
    import time
    from manager import player_tracker

    tracker = player_tracker.get_tracker()
    notify = tracker.subscribe()

    log.debug("SSE /map/api/players/stream subscriber from %s "
              "(total subs now %d)",
              request.remote_addr, tracker.subscriber_count())

    def generate():
        try:
            yield _SSE_RETRY_FRAME
            yield _SSE_INITIAL_PADDING
            # Send the current state immediately on connect so the
            # frontend doesn't wait up to 30 sec for the first push.
            yield _sse_players_frame(tracker.get_state())

            last_keepalive = time.monotonic()
            while True:
                # Wait for a tracker notification or until the
                # keepalive interval expires.
                woke = notify.wait(timeout=_SSE_KEEPALIVE_INTERVAL_SEC)
                notify.clear()
                if woke:
                    yield _sse_players_frame(tracker.get_state())
                    last_keepalive = time.monotonic()
                if time.monotonic() - last_keepalive >= _SSE_KEEPALIVE_INTERVAL_SEC:
                    yield _SSE_KEEPALIVE
                    last_keepalive = time.monotonic()
        except GeneratorExit:
            pass
        finally:
            tracker.unsubscribe(notify)

    response = Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",  # nginx hint -- harmless without nginx
        "Connection": "keep-alive",
    })
    response.direct_passthrough = True
    return response
