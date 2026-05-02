"""Game-event parser.

Subscribes to WS.log / WS_2.log tailers, parses lines into structured
GameEvent objects, and broadcasts them to its own subscribers via the same
interface LogTailer uses. That lets the existing /logs SSE endpoint serve
the events tab without any plumbing changes.

Event format on the wire (and in the UI):

    <actor> : <kind> : <summary>

For player events, actor is the in-game character name (or Steam username
for joins before a character is picked).

Patterns are sourced from observed live-server output (April 2026, after
Set_OutputChats=1). See memory: log_event_patterns.md.

To add a new event type:
  1. Add a regex constant here.
  2. Add a parse step in `_parse_line()` (first-match-wins).
  3. The events tab picks it up automatically.

Future hooks (not yet implemented):
  - structured GameEvent buffer (`get_event_history()`) for Discord webhook
    forwarding, audit log persistence, milestone broadcast triggers, etc.
"""

import logging
import json
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)


@dataclass
class GameEvent:
    ts: float
    server: str           # tailer name: "ws" / "ws_2"
    actor: str            # player name, tribe name, or "SYSTEM"
    actor_kind: str       # "player" / "tribe" / "system"
    kind: str             # "joined" / "left" / "chat" / "died" / "knocked down" / ...
    summary: str
    raw: dict = field(default_factory=dict)

    def as_line(self) -> str:
        """The simple text-log format the UI displays."""
        return f"{self.actor} : {self.kind} : {self.summary}"


# ── Patterns ────────────────────────────────────────────────────────────────


RX_JOIN = re.compile(
    r"logStoreGamemode: player ready\. "
    r"Addr:(?P<ip>[\d.]+), "
    r"Netuid:(?P<sid>\d+), "
    r"Name:(?P<name>.+?)$"
)
RX_LEAVE = re.compile(
    r"logStoreGamemode: Display: player leave world\. (?P<sid>\d+)"
)
RX_CHAT = re.compile(
    r"logWorldChat: Display: \[(?P<channel>[^,]*),(?P<name>.+?)\((?P<sid>\d+)\)\](?P<text>.*)$"
)
RX_DEATH_VICTIM = re.compile(
    r"LogWS: Warning: 死亡日志 : You Are Dead, Name = (?P<victim>.+?) \( "
)
RX_DEATH_KILLER = re.compile(
    r"Killer = (?P<killer>.+?) \(\(?.*?\)?\)"
)
RX_DEATH_GA = re.compile(r"GA = (?P<ga>\S+)")
# Empty kill-meta brackets = NPC kill. The full death line ends with
# "Killer = X () [ ], GA = ..." for wildlife/wild NPCs, or
# "Killer = Player (( Player )) [主人:(SteamId:...)] ..." for players.
# So `[ ]` (empty bracket pair, with the space) only appears for NPC kills.
RX_DEATH_NPC_MARKER = re.compile(r"\[ \]")

RX_KNOCKDOWN_BEGIN = re.compile(
    # PLAYER knockdown only -- BP_*PlayerBase* in the BP class. Without the
    # PlayerBase filter we'd flood with every NPC knockout (which precedes
    # a recruit attempt).
    r"LogWS: Warning: ============== (?P<name>.+?) \( .+? \) "
    r"BP: \S*PlayerBase\S* 濒死 Begin"
)
RX_THRALL_DOWN = re.compile(
    # THRALL knockdown -- the ownership format uses ANGLE brackets:
    #   ============== Vagabond < ExamplePlayer > BP: BP_..._Base_F_C_NNNN ...
    # vs. wild NPC which uses round brackets:
    #   ============== Vagabond ( Vagabond ) BP: ...
    r"LogWS: Warning: ============== (?P<thrall>.+?) < (?P<owner>.+?) > "
    r"BP: \S+ 濒死 Begin"
)
RX_RECRUIT = re.compile(
    # ZhaoMuChanged Add fires on actual recruits AND on player respawns
    # (the player's own PlayerBase NPC re-enters their tribe). The Reason
    # field distinguishes them: real recruits have no respawn marker,
    # respawns have `AHPlayerState::ChongSheng [Respawn]`. Capture Reason
    # so the parser can gate on it.
    r"LogWS: ZhaoMuChanged Add : "
    r"PlayerName\[(?P<player>.+?)\] "
    r"CharacterName\[(?P<recruit>.+?)\] "
    r"Name\[(?P<bp>.+?)\] "
    r"SourceGuid\[(?P<guid>[A-F0-9]+)\] "
    r"Reason:\[(?P<reason>.*?)\]"
)
RX_THRALL_LOST = re.compile(
    # Inverse of recruit: thrall removed from tribe (died, dismissed, etc.).
    # The PLAYER's own death also fires this (PlayerBase BP) -- we filter
    # those out in code below since they're redundant with the death event.
    r"LogWS: ZhaoMuChanged Delete : "
    r"PlayerName\[(?P<player>.+?)\] "
    r"CharacterName\[(?P<thrall>.+?)\] "
    r"Name\[(?P<bp>.+?)\] "
    r"SourceGuid\[(?P<guid>[A-F0-9]+)\] "
    r"Reason:\[(?P<reason>.*?)\]"
)

# ── New patterns (added after deep log audit) ──────────────────────────────

# RiZhi family: a generic prefix matcher then per-type Params shape.
# All RiZhi lines share `GongHuiName[<tribe>] RiZhiType: <N> Params : ...`.
# We pull the tribe out for tribe-attributable events (demolish, raid).

RX_HOOK = re.compile(
    # Server-side mod marker. Anything emitted via Soulmask devkit's
    # Log String node with a literal `[HOOK] ...` body lands in
    # WS.log under category `LogBlueprintUserMessages`. Capture
    # everything after `[HOOK] ` -- caller decides whether the body
    # is plain text (free-form dump) or JSON ({"v":1,"t":"...","data":...}).
    # Anchored to end-of-line so the captured string is the full payload.
    r"\[HOOK\]\s+(?P<payload>.+?)\s*$"
)

RX_RIZHI_KILL = re.compile(
    # Type 14: a kill by player or thrall.
    #   Params : <player> ( <player> )   <victim>            -- player kill
    #   Params : <thrall> < <owner> >    <victim>            -- thrall kill (credit owner)
    r"GongHuiName\[(?P<tribe>.*?)\] RiZhiType: 14 Params : "
    r"(?P<actor>.+?)   (?P<victim>.+?)\s+LogExtParam"
)
RX_ACTOR_PLAYER = re.compile(r"^(?P<player>.+?) \( .+? \)$")
RX_ACTOR_THRALL = re.compile(r"^(?P<thrall>.+?) < (?P<owner>.+?) >$")

RX_RIZHI_BUILT = re.compile(
    # Type 17: building placed.
    #   Params : <player>   <building>
    r"RiZhiType: 17 Params : (?P<player>.+?)   (?P<building>.+?)\s+LogExtParam"
)
RX_RIZHI_DEMOLISHED = re.compile(
    # Type 19: building demolished. No actor in params -- use tribe name from
    # the GongHuiName field instead.
    r"GongHuiName\[(?P<tribe>.*?)\] RiZhiType: 19 Params : "
    r"(?P<building>.+?)\s+LogExtParam"
)
RX_RIZHI_RAIDED = re.compile(
    # Type 18: invader damaging tribe property (fires repeatedly during a raid).
    #   Params : <invader>   <building_target>
    r"GongHuiName\[(?P<tribe>.*?)\] RiZhiType: 18 Params : "
    r"(?P<invader>.+?)   (?P<target>.+?)\s+LogExtParam"
)
RX_RIZHI_ENCLOSURE = re.compile(
    # Type 62: animal pen / enclosure event (placed / housed / interacted).
    #   Params : <player>   <enclosure>
    r"RiZhiType: 62 Params : (?P<player>.+?)   (?P<enclosure>.+?)\s+LogExtParam"
)

RX_TELEPORT_GATE = re.compile(
    # Player using a teleport gate. Internal NPC / AI teleports use other
    # FunNames (DelayTeleportToNearestYingHuo, UHBTService_*) -- explicitly
    # match GameFunctionSinglePointTrans only so we don't flood with NPC noise.
    r"LogWS: Warning: 传送日志 :FunName = "
    r"AHJianZhuGameFunction::GameFunctionSinglePointTrans \d+, "
    r"Name = (?P<name>.+?), "
    r"LanTuName = (?P<bp>\S+), "
    r"Teleport From \[(?P<from>[^\]]+)\] To \[(?P<to>[^\]]+)\]"
)

RX_LOOT_DROP = re.compile(
    # Item dropped from a kill.
    #   Killer is in the same `<player> (( <char> ))` self-paren format the
    #   death log uses for player-as-killer.
    r"LogWS: Warning: 掉落日志:"
    r"Name= (?P<item>.+?), "
    r"Loc= \([^)]+\), "
    r"Killer= (?P<killer>.+?), "
    r"DiaoLuoBao= (?P<table>\S+)"
)
RX_LOOT_KILLER_NAME = re.compile(r"^(?P<player>.+?) \(\(?")

RX_NEW_CHARACTER = re.compile(
    # Fires when a player creates / re-creates a character. After death
    # respawn you appear as a fresh character (sometimes a new name).
    r"LogWS: Server AlReady CreateRoleName: (?P<name>.+?)$"
)

# ── Invasion / raid phase transitions ──────────────────────────────────────
# The four-phase Fever -> Preparation -> Scouting -> Attack -> Settlement
# state machine. Three of the four phases produce explicit log lines; the
# Attack transition has no line and is detected downstream (in the Discord
# relay) from the first RiZhi 18 damage event that fires while a raid is
# active.

RX_INVASION_PREP = re.compile(
    # Preparation phase: server picks target tribe, source point, target
    # point, building count, and creature count. One line per raid.
    r"LogWS:\s+入侵进入准备阶段!\s*"
    r"建筑数:(?P<buildings>\d+),\s*"
    r"生物数:(?P<creatures>\d+)!\s*"
    r"起始点原因:(?P<source_reason>\S+)!\s*"
    r"起始点:(?P<sx>-?\d+)\s+(?P<sy>-?\d+)\s+(?P<sz>-?\d+)!\s*"
    r"目标点:(?P<tx>-?\d+)\s+(?P<ty>-?\d+)\s+(?P<tz>-?\d+)"
)
RX_INVASION_SCOUT = re.compile(
    # Scouting phase: a single scout NPC walks toward the player base.
    # Players can interrogate it for map intel; killing it forfeits the
    # intel but doesn't cancel the raid.
    r"LogWS:\s+入侵进入探查阶段[,，]?在线玩家至少有\s*:\s*(?P<players>.+?)$"
)
RX_INVASION_SETTLE = re.compile(
    # Settlement: outcome of the raid. "Win Clear GuaiWu" = defenders
    # cleared all raiders. "Failed Timeout" = invaders failed to clear
    # the timer (often: spawned somewhere they couldn't path from).
    # WORDING TRAP: "Failed" = invaders failed = defender wins. Don't
    # render this as defeat.
    r"LogWS:\s+RuQin\s+EnterJieSuanStage\s+"
    r"(?P<outcome>Win\s+Clear\s+GuaiWu|Failed\s+Timeout)\s*:\s*"
    r"(?P<guild_id>[A-F0-9]+)\s*-\s*(?P<raid_id>\d+)"
)

# ── Location extraction (for future map / heat-spot views) ──────────────────
# Two formats appear in Soulmask logs depending on the source:
#   LogExtParam: Location :[X=194186.469 Y=200677.703 Z=31663.148]
#   Loc= (-96368, 60691, 35196)
RX_LOC_BRACKETS = re.compile(
    r"Location\s*:\s*\[X=(?P<x>[-\d.]+)\s+Y=(?P<y>[-\d.]+)\s+Z=(?P<z>[-\d.]+)\]"
)
RX_LOC_PARENS = re.compile(
    r"Loc=\s*\((?P<x>[-\d.]+),\s*(?P<y>[-\d.]+),\s*(?P<z>[-\d.]+)\)"
)
RX_KILL_CHAR_LOCATION = re.compile(
    # Some kill events also include the KILLER's position separately.
    r"KillCharLocation\s*:\s*\[X=(?P<x>[-\d.]+)\s+Y=(?P<y>[-\d.]+)\s+Z=(?P<z>[-\d.]+)\]"
)


def _extract_location(line: str) -> Optional[dict]:
    """Pull (x, y, z) coordinates from a log line in either format. Returns
    None if no location is present. Floats so they survive JSON round-trips."""
    m = RX_LOC_BRACKETS.search(line) or RX_LOC_PARENS.search(line)
    if not m:
        return None
    return {
        "x": float(m.group("x")),
        "y": float(m.group("y")),
        "z": float(m.group("z")),
    }


def _extract_kill_char_location(line: str) -> Optional[dict]:
    m = RX_KILL_CHAR_LOCATION.search(line)
    if not m:
        return None
    return {
        "x": float(m.group("x")),
        "y": float(m.group("y")),
        "z": float(m.group("z")),
    }


def _classify_rizhi_actor(raw: str) -> tuple[str, str, str]:
    """Disambiguate a RiZhi 'actor' field. Returns (actor, kind, original).

    actor   -- the human-readable name to use as event actor
    kind    -- "player" | "thrall" | "unknown"
    original-- the raw string (for events where the thrall vs player split matters
               for summary text)
    """
    m = RX_ACTOR_PLAYER.match(raw)
    if m:
        return m.group("player").strip(), "player", raw
    m = RX_ACTOR_THRALL.match(raw)
    if m:
        # Credit the OWNER as the actor (they're the one who'll be notified)
        return m.group("owner").strip(), "thrall", raw
    return raw.strip(), "unknown", raw


def _simplify_ga(ga: str) -> str:
    """Trim Soulmask's verbose ability identifiers down to a readable token."""
    if not ga:
        return ""
    s = ga
    if s.startswith("Default__GA_"):
        s = s[len("Default__GA_"):]
    if s.endswith("_C"):
        s = s[:-2]
    return s


# ── Parser dispatch ─────────────────────────────────────────────────────────


def _parse_line(ts: float, line: str, server: str,
                sessions: dict[str, str],
                recent_deaths: Optional[dict[str, float]] = None
                ) -> Optional[GameEvent]:
    """Try each parser; first match wins. `sessions` is a per-server map of
    SteamID -> name, used so 'leave' (which only logs SteamID) can resolve a
    human-readable actor.

    `recent_deaths` is a per-server map of player_name -> last-death-ts,
    used to suppress the spurious Type-14 'killed' echo that Soulmask
    emits whenever a player dies (the death log fires AND a Type-14
    line fires with the same `<player>(<player>) <killer>` format used
    for real kills). When a Type-14 fires within ~5 sec of the player's
    own death, we drop it -- the death event already covered it."""
    if recent_deaths is None:
        recent_deaths = {}

    # ── [HOOK] from a server-side mod's Log String node ──
    # First-match-wins parser; HOOK lines are unambiguous (no other
    # pattern below would match `[HOOK] ...`), so checking it first
    # short-circuits cheap. Body can be free-form text (during the
    # dump-and-iterate phase the operator is in now) or eventually
    # `{"v":1,"t":"<event>","data":{...}}` once the wire format is
    # locked in. We try JSON-parse opportunistically -- on success,
    # the parsed dict is exposed in `raw.json` so future structured
    # dispatch can read fields without re-parsing.
    m = RX_HOOK.search(line)
    if m:
        payload = m.group("payload").strip()
        raw: dict = {"text": payload}
        if payload.startswith("{") and payload.endswith("}"):
            try:
                parsed = json.loads(payload)
                if isinstance(parsed, dict):
                    raw["json"] = parsed
            except json.JSONDecodeError:
                # Not JSON -- the body just happened to start with `{`.
                # Leave it as text; nothing to do.
                pass
        return GameEvent(
            ts=ts, server=server, actor="MOD", actor_kind="system",
            kind="hook",
            summary=payload[:500],
            raw=raw,
        )

    # ── Join ──
    m = RX_JOIN.search(line)
    if m:
        sid = m.group("sid")
        name = m.group("name").strip()
        sessions[sid] = name
        return GameEvent(
            ts=ts, server=server, actor=name, actor_kind="player",
            kind="joined",
            summary="connected",
            raw={"steam_id": sid, "ip": m.group("ip"), "name": name},
        )

    # ── Leave ──
    m = RX_LEAVE.search(line)
    if m:
        sid = m.group("sid")
        # Prefer the in-memory session map (most recent observed name).
        # If the join scrolled out of the tailer history before the
        # manager attached, fall back to the players_db -- it persists
        # across restarts. Only show the steam-id placeholder when the
        # player has truly never been seen.
        name = sessions.get(sid)
        if not name:
            try:
                from manager import players_db
                name = players_db.display_name(sid)
            except Exception:
                name = None
        if not name:
            name = f"<steam:{sid}>"
        return GameEvent(
            ts=ts, server=server, actor=name, actor_kind="player",
            kind="left",
            summary="disconnected",
            raw={"steam_id": sid, "name": name},
        )

    # ── Chat ──
    m = RX_CHAT.search(line)
    if m:
        channel = (m.group("channel") or "").strip()
        name = m.group("name").strip()
        text = m.group("text").strip()
        # World chat has empty channel between [ and , -- relabel to "world"
        # for readability. Other channels (Tribe, Whisper, etc.) keep their name.
        kind = "chat" if not channel else f"chat ({channel})"
        return GameEvent(
            ts=ts, server=server, actor=name, actor_kind="player",
            kind=kind,
            summary=text,
            raw={"steam_id": m.group("sid"), "name": name,
                 "channel": channel or "world", "text": text},
        )

    # ── Death (only first death-log line; ignore the 死亡日志2 follow-up) ──
    if "死亡日志 :" in line:
        m_v = RX_DEATH_VICTIM.search(line)
        m_k = RX_DEATH_KILLER.search(line)
        if m_v and m_k:
            victim = m_v.group("victim").strip()
            killer = m_k.group("killer").strip()
            mga = RX_DEATH_GA.search(line)
            ga = mga.group("ga") if mga else ""
            ga_simple = _simplify_ga(ga)
            is_npc = bool(RX_DEATH_NPC_MARKER.search(line))

            if killer == victim:
                summary = "died (self / environment)"
            elif ga_simple:
                summary = f"killed by {killer} ({ga_simple})"
            else:
                summary = f"killed by {killer}"

            # Record this death so the Type-14 echo (which fires
            # within ~5ms of the death log) gets suppressed below.
            recent_deaths[victim] = ts

            return GameEvent(
                ts=ts, server=server, actor=victim, actor_kind="player",
                kind="died",
                summary=summary,
                raw={"victim": victim, "killer": killer, "ga": ga,
                     "is_npc": is_npc},
            )

    # ── Knockdown (the 濒死 Begin marker line) ──
    m = RX_KNOCKDOWN_BEGIN.search(line)
    if m:
        name = m.group("name").strip()
        return GameEvent(
            ts=ts, server=server, actor=name, actor_kind="player",
            kind="knocked down",
            summary="entered dying state",
            raw={"name": name},
        )

    # ── Recruitment (player adds an NPC tribesman to their tribe) ──
    m = RX_RECRUIT.search(line)
    if m:
        reason = m.group("reason").strip()
        # ZhaoMuChanged Add ALSO fires on player respawn -- the
        # player's own PlayerBase NPC rejoins the tribe each time they
        # come back from death, with Reason "AHPlayerState::ChongSheng
        # [Respawn]". Without this gate the Discord relay sends a
        # "recruited" message every time someone dies and respawns,
        # naming the player as both recruiter and recruit. Trust the
        # Reason field as the authoritative signal -- ChongSheng is
        # the in-game Chinese for "respawn"; matching the substring
        # also catches any future Reason variants that include the
        # token, and the trailing "[Respawn]" annotation is a second
        # confirmation. Future HookLogger will surface respawns
        # explicitly with location / cause; for now, drop them.
        if "ChongSheng" in reason or "Respawn" in reason:
            return None
        player = m.group("player").strip()
        recruit = m.group("recruit").strip()
        return GameEvent(
            ts=ts, server=server, actor=player, actor_kind="player",
            kind="recruited",
            summary=recruit,
            raw={"player": player, "recruit": recruit,
                 "bp": m.group("bp"), "reason": reason},
        )

    # ── Thrall lost (a recruited NPC was removed from the tribe). The same
    # log line fires for the player's own death (player respawn cycle) --
    # we filter those out by BP class to avoid duplicating the death event.
    m = RX_THRALL_LOST.search(line)
    if m:
        bp = m.group("bp")
        if "PlayerBase" in bp:
            # Player respawn -- redundant with the death event we already
            # emitted; drop.
            return None
        player = m.group("player").strip()
        thrall = m.group("thrall").strip()
        reason = m.group("reason").strip()
        # AHCharacterRen::OnSiWangChanged -> "died"; otherwise show raw reason
        why = "died" if "SiWang" in reason else reason or "lost"
        return GameEvent(
            ts=ts, server=server, actor=player, actor_kind="player",
            kind="thrall lost",
            summary=f"{thrall} ({why})",
            raw={"player": player, "thrall": thrall, "bp": bp,
                 "reason": reason},
        )

    # ── Thrall knocked down (recruited NPC went into dying state) ──
    m = RX_THRALL_DOWN.search(line)
    if m:
        thrall = m.group("thrall").strip()
        owner = m.group("owner").strip()
        return GameEvent(
            ts=ts, server=server, actor=owner, actor_kind="player",
            kind="thrall down",
            summary=f"{thrall} entered dying state",
            raw={"thrall": thrall, "owner": owner},
        )

    # ── New character created (player picked / re-rolled a character) ──
    m = RX_NEW_CHARACTER.search(line)
    if m:
        name = m.group("name").strip()
        return GameEvent(
            ts=ts, server=server, actor=name, actor_kind="player",
            kind="new character",
            summary="created",
            raw={"name": name},
        )

    # ── Invasion: Preparation phase ──
    m = RX_INVASION_PREP.search(line)
    if m:
        buildings = int(m.group("buildings"))
        creatures = int(m.group("creatures"))
        source = {"x": float(m.group("sx")), "y": float(m.group("sy")),
                  "z": float(m.group("sz"))}
        target = {"x": float(m.group("tx")), "y": float(m.group("ty")),
                  "z": float(m.group("tz"))}
        return GameEvent(
            ts=ts, server=server, actor="SYSTEM", actor_kind="system",
            kind="invasion prep",
            summary=(f"Fever maxed -- {creatures} raiders inbound, "
                     f"targeting {buildings} buildings"),
            raw={"buildings": buildings, "creatures": creatures,
                 "source": source, "target": target,
                 "source_reason": m.group("source_reason")},
        )

    # ── Invasion: Scouting phase ──
    m = RX_INVASION_SCOUT.search(line)
    if m:
        # The "online players" list is comma-separated names. Strip
        # leading/trailing whitespace per entry; some logs use a Chinese
        # comma "，" as the field separator before the colon (already
        # handled in the regex), but the player list itself uses ASCII
        # commas.
        players_raw = m.group("players").strip()
        players = [p.strip() for p in players_raw.split(",") if p.strip()]
        return GameEvent(
            ts=ts, server=server, actor="SYSTEM", actor_kind="system",
            kind="invasion scout",
            summary=("Scout dispatched -- interrogate at the bonfire "
                     "for intel before attack begins"),
            raw={"online_players": players},
        )

    # ── Invasion: Settlement phase ──
    m = RX_INVASION_SETTLE.search(line)
    if m:
        # Normalise the outcome string -- whitespace variation in the log
        # line means a literal == match would be fragile. We map to a
        # canonical "won" / "expired" tag.
        outcome_raw = m.group("outcome")
        if "Win" in outcome_raw:
            outcome = "won"
            summary = "Raid defeated -- all raiders cleared"
        else:
            outcome = "expired"
            summary = ("Raid expired -- invaders ran out of time "
                       "(defender wins by default)")
        return GameEvent(
            ts=ts, server=server, actor="SYSTEM", actor_kind="system",
            kind="invasion settle",
            summary=summary,
            raw={"outcome": outcome,
                 "guild_id": m.group("guild_id"),
                 "raid_id": m.group("raid_id")},
        )

    # ── Teleport gate use (player only -- regex already filters out NPC pathing) ──
    m = RX_TELEPORT_GATE.search(line)
    if m:
        # Belt + braces: the FunName already filters out NPC AI teleports,
        # but also confirm the BP is a player class before crediting.
        bp = m.group("bp")
        if "PlayerBase" in bp:
            name = m.group("name").strip()
            return GameEvent(
                ts=ts, server=server, actor=name, actor_kind="player",
                kind="teleported",
                summary="used a teleport gate",
                raw={"name": name, "bp": bp,
                     "from": m.group("from"), "to": m.group("to")},
            )

    # ── Loot drop from a kill ──
    m = RX_LOOT_DROP.search(line)
    if m:
        item = m.group("item").strip()
        # Strip the "<  >" suffix the item name often carries (item-rarity tier).
        item = re.sub(r"\s*<\s*>\s*$", "", item).strip()
        killer_raw = m.group("killer").strip()
        km = RX_LOOT_KILLER_NAME.match(killer_raw)
        killer = km.group("player").strip() if km else killer_raw
        loc = _extract_location(line)
        raw = {"player": killer, "item": item, "table": m.group("table")}
        if loc:
            raw["location"] = loc
        return GameEvent(
            ts=ts, server=server, actor=killer, actor_kind="player",
            kind="looted",
            summary=item,
            raw=raw,
        )

    # ── RiZhi-based events (catch-all -- match these LAST, after the more
    # specific LogWS lines, since several RiZhi types share the same prefix) ──

    # Type 14: kill (player or thrall as actor)
    #
    # CAVEAT: when a player DIES, Soulmask emits BOTH a 死亡日志 line
    # (correctly handled above as 'died') AND a Type 14 entry with
    # the same `<player>(<player>) <other>` format used for real
    # kills. We can't distinguish from the line alone -- the
    # disambiguator is timing: a Type 14 immediately after the
    # player's own death is the death echo, not a kill.
    #
    # Window: 1.0s. The two log lines are written in the same tick
    # by the game; a generous 5s window was the original guess, but
    # that's wide enough to swallow a legitimate retaliation kill if
    # the player respawns and immediately kills the mob that killed
    # them. 1s covers the same-tick echo case without risking real
    # kills being dropped.
    m = RX_RIZHI_KILL.search(line)
    if m:
        actor_raw = m.group("actor").strip()
        victim = m.group("victim").strip()
        actor, kind, _ = _classify_rizhi_actor(actor_raw)
        # Suppress the death echo: if `actor` died very recently,
        # this Type 14 is the death-side log entry, not a real kill.
        if kind == "player":
            last_death = recent_deaths.get(actor, 0)
            if last_death and (ts - last_death) <= 1.0:
                # Drop this event silently. The 'died' event already
                # captured the incident.
                return None
        loc = _extract_location(line)            # victim's death location
        kill_loc = _extract_kill_char_location(line)  # killer's location
        if kind == "player":
            raw = {"player": actor, "victim": victim}
            if loc: raw["location"] = loc
            if kill_loc: raw["killer_location"] = kill_loc
            return GameEvent(
                ts=ts, server=server, actor=actor, actor_kind="player",
                kind="killed",
                summary=victim,
                raw=raw,
            )
        elif kind == "thrall":
            tm = RX_ACTOR_THRALL.match(actor_raw)
            thrall_name = tm.group("thrall").strip() if tm else "thrall"
            raw = {"owner": actor, "thrall": thrall_name, "victim": victim}
            if loc: raw["location"] = loc
            if kill_loc: raw["killer_location"] = kill_loc
            return GameEvent(
                ts=ts, server=server, actor=actor, actor_kind="player",
                kind="thrall killed",
                summary=f"{victim} (via {thrall_name})",
                raw=raw,
            )
        return None

    # Type 17: building placed
    m = RX_RIZHI_BUILT.search(line)
    if m:
        player = m.group("player").strip()
        building = m.group("building").strip()
        return GameEvent(
            ts=ts, server=server, actor=player, actor_kind="player",
            kind="built",
            summary=building,
            raw={"player": player, "building": building},
        )

    # Type 18: invader damaging tribe property (raid event)
    m = RX_RIZHI_RAIDED.search(line)
    if m:
        tribe = m.group("tribe").strip() or "(no tribe)"
        invader = m.group("invader").strip()
        target = m.group("target").strip()
        return GameEvent(
            ts=ts, server=server, actor=tribe, actor_kind="tribe",
            kind="raided",
            summary=f"{invader} -> {target}",
            raw={"tribe": tribe, "invader": invader, "target": target},
        )

    # Type 19: building demolished (no actor in params; use tribe name)
    m = RX_RIZHI_DEMOLISHED.search(line)
    if m:
        tribe = m.group("tribe").strip() or "(no tribe)"
        building = m.group("building").strip()
        return GameEvent(
            ts=ts, server=server, actor=tribe, actor_kind="tribe",
            kind="demolished",
            summary=building,
            raw={"tribe": tribe, "building": building},
        )

    # Type 62: animal pen / enclosure event
    m = RX_RIZHI_ENCLOSURE.search(line)
    if m:
        player = m.group("player").strip()
        enclosure = m.group("enclosure").strip()
        return GameEvent(
            ts=ts, server=server, actor=player, actor_kind="player",
            kind="enclosure",
            summary=enclosure,
            raw={"player": player, "enclosure": enclosure},
        )

    return None


# ── Stream (LogTailer-compatible interface) ─────────────────────────────────


class EventStream:
    """Aggregates events from multiple log tailers. Has the same subscribe /
    get_history / subscriber_count surface as LogTailer, so the existing
    /logs SSE infrastructure works for it unchanged."""

    def __init__(self, history_size: int = 500):
        self._history: deque[tuple[float, str]] = deque(maxlen=history_size)
        self._events: deque[GameEvent] = deque(maxlen=history_size)
        self._subscribers: list[Callable[[float, str], None]] = []
        # Separate subscriber list that gets the full GameEvent (not the
        # rendered line). Used by the Discord relay + players_db integration
        # which need event.kind / event.raw, not just the human-readable
        # "actor : kind : summary" string.
        self._event_subscribers: list[Callable[[GameEvent], None]] = []
        self._lock = threading.Lock()
        # Per-server session maps -- keyed by tailer name so cluster nodes
        # don't share / collide. Each map: SteamID -> last-seen name.
        self._sessions: dict[str, dict[str, str]] = {}
        # Per-server recent-death tracker for the Type-14 echo
        # disambiguation (player_name -> ts of last death). See
        # _parse_line docstring for context.
        self._recent_deaths: dict[str, dict[str, float]] = {}

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def subscribe(self, callback) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)
        return unsubscribe

    def subscribe_events(self,
                         callback: Callable[["GameEvent"], None]
                         ) -> Callable[[], None]:
        """Subscribe to fully-typed GameEvent objects (not the rendered
        line). Use this when you need event.kind / event.raw / etc. --
        e.g. Discord relay, players_db journal writes, milestone
        triggers."""
        with self._lock:
            self._event_subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._event_subscribers:
                    self._event_subscribers.remove(callback)
        return unsubscribe

    def get_history(self) -> list[tuple[float, str]]:
        with self._lock:
            return list(self._history)

    def get_event_history(self) -> list[GameEvent]:
        """Structured history. Future hook for Discord forwarding / audit."""
        with self._lock:
            return list(self._events)

    def attach_to(self, tailer, server_name: str) -> None:
        """Subscribe to a LogTailer. Replays the tailer's existing history
        through the parsers first (so the events tab has context immediately
        on a fresh manager start), then subscribes for live updates."""
        sessions = self._sessions.setdefault(server_name, {})
        recent_deaths = self._recent_deaths.setdefault(server_name, {})

        # Replay buffered history.
        for ts, line in tailer.get_history():
            event = _parse_line(ts, line, server_name, sessions, recent_deaths)
            if event:
                self._emit(event)

        # Subscribe for live.
        def cb(ts: float, line: str) -> None:
            event = _parse_line(ts, line, server_name, sessions, recent_deaths)
            if event:
                self._emit(event)

        tailer.subscribe(cb)
        log.info("EventStream attached to tailer %r (replayed %d historical "
                 "lines for context)", server_name, len(tailer.get_history()))

    def _emit(self, event: GameEvent) -> None:
        line = event.as_line()
        with self._lock:
            self._events.append(event)
            self._history.append((event.ts, line))
            line_subs = list(self._subscribers)
            event_subs = list(self._event_subscribers)
        for cb in line_subs:
            try:
                cb(event.ts, line)
            except Exception:
                log.exception("EventStream subscriber raised (line)")
        for cb in event_subs:
            try:
                cb(event)
            except Exception:
                log.exception("EventStream subscriber raised (event)")


# ── Module singleton ────────────────────────────────────────────────────────


_stream: Optional[EventStream] = None
_setup_lock = threading.Lock()


def get_stream() -> Optional[EventStream]:
    return _stream


def setup_event_stream() -> EventStream:
    """Idempotent: create singleton, attach to existing ws and ws_2 tailers."""
    global _stream
    with _setup_lock:
        if _stream is not None:
            return _stream
        _stream = EventStream()

    from manager.tailer import get_tailer
    for tname in ("ws", "ws_2"):
        tailer = get_tailer(tname)
        if tailer is None:
            log.warning("setup_event_stream: tailer %r not yet registered, "
                        "skipping (events from this server will not appear)", tname)
            continue
        _stream.attach_to(tailer, tname)

    log.info("EventStream ready")
    return _stream
