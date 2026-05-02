"""Discord relay integration -- the wiring layer between events,
players_db, and discord_relay.

Responsibilities:

  1. Open the players_db at boot (idempotent connection).
  2. Instantiate a DiscordRelay if `discord.relay_enabled` is True
     and a webhook URL is configured.
  3. Subscribe to the EventStream and on each event:
     a. Append it to the players_db journal (always -- the journal
        feeds future LLM summaries / milestone checks regardless
        of the relay's state).
     b. Open / close player sessions on join / leave.
     c. If the relay is on AND the event's category is enabled,
        format the event and submit it to the relay's queue.

This module is the policy layer. discord_relay.py is policy-free
transport; events.py is a faithful log->event translator. Each
layer does one thing.
"""

import logging
from typing import Optional, Tuple

from manager import discord_relay, events, players_db
from manager.config import get_setting

log = logging.getLogger(__name__)

# Module-level relay instance. None when disabled or unconfigured.
_relay: Optional[discord_relay.DiscordRelay] = None
_started: bool = False


# ── Event -> category mapping ──────────────────────────────────────────────


def _category_setting_key(kind: str) -> Optional[str]:
    """Return the settings key whose bool value gates relay of this
    event kind, or None if the kind is not relayable to Discord.

    Kept event kinds:
      - joined / left  -> discord.relay_joins_leaves
      - died           -> discord.relay_player_deaths (no killer info;
                          WS.log doesn't carry attribution)
      - recruited      -> discord.relay_thrall_captures

    Kinds NOT mapped here -- killed, thrall killed, thrall lost,
    thrall down, invasion phases, knocked down -- are still parsed
    by events.py and journaled by players_db, but no toggle gates
    them so they never reach the Discord relay. Pending HookLogger
    upgrade that will provide reliable replacements (kill attribution,
    invasion data, etc.) via the [HOOK] mod channel.
    """
    if kind in ("joined", "left"):
        return "discord.relay_joins_leaves"
    if kind == "died":
        return "discord.relay_player_deaths"
    if kind == "recruited":
        return "discord.relay_thrall_captures"
    return None


# ── Event handling ──────────────────────────────────────────────────────────


def _resolve_session_id(event: events.GameEvent) -> Optional[int]:
    """Look up the open session for this event's actor (if any).
    Returns None for system events or unknown actors."""
    if event.actor_kind != "player":
        return None
    steam_id = (event.raw or {}).get("steam_id")
    if not steam_id:
        return None
    try:
        return players_db.open_session_id(steam_id)
    except Exception:
        log.exception("open_session_id failed (non-fatal)")
        return None


def _journal(event: events.GameEvent) -> None:
    """Append the event to the players_db journal."""
    try:
        sid = _resolve_session_id(event)
        players_db.append_event(event.kind, event.raw or {},
                                ts=event.ts, session_id=sid)
    except Exception:
        log.exception("players_db.append_event failed (non-fatal)")


def _track_session(event: events.GameEvent) -> None:
    """For join / leave events, open or close the session row."""
    raw = event.raw or {}
    steam_id = raw.get("steam_id")
    if not steam_id:
        return
    try:
        if event.kind == "joined":
            players_db.record_join(steam_id, event.actor, ts=event.ts)
        elif event.kind == "left":
            players_db.record_leave(steam_id, ts=event.ts)
    except Exception:
        log.exception("players_db session tracking failed (non-fatal)")


def _setting_default(key: str, fallback: bool) -> bool:
    """Schema default for `key`, or `fallback` if no schema item.
    The settings page only PERSISTS toggles that differ from schema
    default, so a True-by-default toggle never lands in settings.toml
    when the operator just leaves it on. Using the schema's own default
    (instead of a hardcoded `False`) is what makes the toggle honour
    its UI state at runtime."""
    try:
        from manager import settings_schema
        item = settings_schema.find_item(key)
        if item is not None and isinstance(item.default, bool):
            return item.default
    except Exception:
        pass
    return fallback


def _maybe_relay(event: events.GameEvent) -> None:
    """Submit to the Discord relay if it's running and the event's
    category is enabled in settings.

    Special case: when discord.send_player_join_map is on, the join
    event is handled by the map-image path instead -- skip the
    text-only relay here so the operator only sees ONE message per
    join (text + image attached to the same Discord message). Without
    this guard the operator would see two messages: the text-only
    "Player joined" from here AND the map message from
    _maybe_send_join_map.
    """
    if _relay is None:
        return
    cat = _category_setting_key(event.kind)
    if cat is None:
        return
    if (event.kind == "joined"
            and get_setting("discord.send_player_join_map", False)):
        return
    # Default tracks the schema (True for joins/leaves, deaths,
    # captures). Without this, a True-by-default toggle that the
    # operator never explicitly changed reads as False at runtime
    # and silently drops the event.
    enabled = bool(get_setting(cat, _setting_default(cat, False)))
    log.debug("discord relay gate: kind=%s key=%s enabled=%s relay=%s",
              event.kind, cat, enabled, _relay is not None)
    if not enabled:
        return
    msg = discord_relay.format_event(event)
    if msg:
        try:
            _relay.submit(msg)
        except Exception:
            log.exception("discord_relay.submit failed (non-fatal)")


def _resolve_player_location(steam_id: str) -> tuple[Optional[dict], str]:
    """Resolve a player's current/recent UE coords.

    Tries in order:
      1. EchoPort `lp` against every running game-server instance --
         returns CURRENT coords for online players. Cheap (~250 bytes).
      2. players_db.last_known_location -- their most recent journaled
         event with a location. Approximate but covers offline / lag.

    Returns (loc_dict, source_label) where source_label is one of
    'echoport-live', 'journal', 'none'. loc_dict has at least
    {x, y, z}; the journal version also has {at, kind}."""
    # 1. Live coords via EchoPort across all running instances.
    # running_instances() yields (RuntimeInstance, InstanceProcess) tuples.
    try:
        from manager import echo, lifecycle
        for ri, _proc in lifecycle.running_instances():
            port = getattr(ri.instance, "echo_port", None)
            if not port:
                continue
            players = echo.list_online_players("127.0.0.1", port)
            if not players:
                continue
            for p in players:
                if p["steam_id"] == steam_id:
                    return ({"x": p["x"], "y": p["y"], "z": p["z"]},
                            "echoport-live")
    except Exception:
        log.exception("echoport lp lookup failed (non-fatal)")

    # 2. Journal fallback
    try:
        loc = players_db.last_known_location(steam_id)
        if loc:
            return (loc, "journal")
    except Exception:
        log.exception("last_known_location failed (non-fatal)")

    return (None, "none")


def _maybe_send_join_map(event: events.GameEvent) -> None:
    """When a player joins AND `discord.send_player_join_map` is on,
    enqueue the join-map render onto the background worker. The
    actual work (EchoPort lp lookup, Pillow tile compositing,
    Discord submit) happens off the event-handler hot path; this
    function only does cheap gating so it's safe to call from the
    tailer thread on every event.

    Gated by both the master `relay_joins_leaves` toggle (since the
    map message replaces the text join message, you can't have it
    on without joins/leaves on) AND the map-specific toggle.

    The slow work used to run inline here, which made the WS.log
    tailer thread block on disk I/O / network for tens of ms per
    join. With the queue, joins enqueue in microseconds and the
    bg-worker drains them serially.
    """
    if _relay is None:
        return
    if event.kind != "joined":
        return
    if not get_setting("discord.relay_joins_leaves",
                       _setting_default("discord.relay_joins_leaves", True)):
        return
    if not get_setting("discord.send_player_join_map", False):
        return
    raw = event.raw or {}
    steam_id = raw.get("steam_id")
    if not steam_id:
        return

    from manager import background
    background.submit("discord-join-map", _send_join_map_impl, event, steam_id)


def _send_join_map_impl(event: events.GameEvent, steam_id: str) -> None:
    """Body of _maybe_send_join_map. Runs on the bg-worker thread so
    EchoPort I/O + Pillow compositing don't stall the EventStream.

    Message content is intentionally minimal -- a single line matching
    the text-only join format ('+ Player joined the server') with the
    map image attached. No coords / source / region in the text;
    that info is in the picture, no need to duplicate.
    """
    # Map the tailer name (event.server: "ws" / "ws_2") to the Soulmask
    # level identifier so we render against the correct tile pyramid.
    # Falls back to DLC if the lookup fails (settings not loadable yet,
    # unknown server name) -- DLC is the most likely user-facing map.
    from manager.paths import level_for_server
    level = level_for_server(event.server) or "DLC_Level01_Main"

    loc, source = _resolve_player_location(steam_id)

    try:
        from manager import map_render
        if loc:
            png = map_render.composite_player_view(
                level,
                pos_x=loc["x"], pos_y=loc["y"],
                marker_label=event.actor,
            )
            # Tiles missing for this viewport -- fall back to overview.
            if png is None:
                png = map_render.composite_overview(level)
        else:
            # No coords (first-time joiner pre-journal, or lookup
            # failed) -- whole-map overview keeps the message helpful.
            png = map_render.composite_overview(level)

        if png is None:
            log.warning("map_render: no image produced for join of %s; "
                        "skipping image send", event.actor)
            return
        log.info("discord: sending join-map image for %s "
                 "(coord-source=%s, %d KB)",
                 event.actor, source, len(png) // 1024)
        msg = discord_relay.RelayMessage(
            content=f"➕ **{event.actor}** joined the server",
            image_bytes=png,
            image_filename=f"join_{event.actor.replace(' ', '_')}.png",
        )
        _relay.submit(msg)
    except ImportError as e:
        log.warning("discord: cannot render join map -- %s. "
                    "Did you `pip install Pillow`?", e)
    except Exception:
        log.exception("discord: join-map render failed (non-fatal)")


def _on_event(event: events.GameEvent) -> None:
    """Single subscriber that fans out to the three handlers. Order
    matters slightly: open the session BEFORE journaling so the
    journal entry can attach to the just-created session id; close
    the session AFTER journaling so the leave event itself is
    journalled inside the session about to close."""
    if event.kind == "joined":
        _track_session(event)
        _journal(event)
    elif event.kind == "left":
        _journal(event)
        _track_session(event)
    else:
        _journal(event)
    _maybe_relay(event)
    # Image-on-join: separate code path so a Pillow / tile failure
    # doesn't break text relay. Internally checks the opt-in setting
    # and the event kind.
    _maybe_send_join_map(event)


# ── Lifecycle ──────────────────────────────────────────────────────────────


def start() -> None:
    """Boot-time: open the players DB, start the Discord relay if
    configured, and subscribe to the EventStream. Idempotent --
    calling twice does nothing the second time."""
    global _relay, _started
    if _started:
        return
    _started = True

    # 1. Players DB always opens (journal is independent of Discord).
    try:
        players_db.open_db()
        log.info("Discord integration: players DB opened")
    except Exception:
        log.exception("players_db open failed at boot")

    # 2. Discord relay starts only if enabled AND URL configured.
    if not get_setting("discord.relay_enabled", False):
        log.info("Discord relay: disabled in settings")
    else:
        url = get_setting("discord.webhook_url", "")
        if not url:
            log.warning("Discord relay: enabled but webhook_url is "
                        "empty; not starting worker. Set the URL "
                        "on /settings/ then restart the manager.")
        else:
            interval = float(get_setting("discord.batch_interval_sec", 10))
            try:
                _relay = discord_relay.DiscordRelay(
                    webhook_url=url, batch_interval_sec=interval)
                _relay.start()
                log.info("Discord relay: started "
                         "(batch_interval=%.1fs)", interval)
            except Exception:
                log.exception("Discord relay: failed to start; "
                              "events will only journal, not relay")
                _relay = None

    # 3. Subscribe to the event stream regardless of whether the relay
    #    is active. The journal write happens unconditionally so the
    #    LLM-summary phase has data even if Discord isn't configured.
    stream = events.get_stream()
    if stream is None:
        log.warning("Discord integration: EventStream not yet ready -- "
                    "no events will be journaled or relayed until it is")
        return
    stream.subscribe_events(_on_event)
    log.info("Discord integration: subscribed to EventStream "
             "(relay=%s)", "ON" if _relay else "OFF")


def stop() -> None:
    """Stop the relay worker. Used on graceful shutdown."""
    global _relay
    if _relay is not None:
        try:
            _relay.stop()
        except Exception:
            log.exception("Discord relay stop failed")
        _relay = None
