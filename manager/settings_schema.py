"""Schema for the operator-editable settings page.

Single source of truth for: what tunables exist, where they live in
settings.toml, their type/range/default, and a short human label.

The page renders sections in the order declared. Each item declares whether
a manager restart is needed for the change to take effect (we display a
note in the UI but never auto-restart).

Add new tunables here and they appear on the page automatically.
"""

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class SettingItem:
    key: str                 # dotted path in settings.toml, e.g. "ui.dashboard_refresh_sec"
    label: str
    kind: str                # "int" | "str" | "choice" | "bool"
    default: Any
    help: str = ""
    min: Optional[int] = None
    max: Optional[int] = None
    choices: Optional[list[str]] = None  # only for kind="choice"
    restart_required: bool = False
    # For kind="bool" only: keys this checkbox gates. When the checkbox is
    # off, the listed inputs are visually disabled (readonly + greyed)
    # but still submit their existing value, so toggling off->on doesn't
    # lose the operator's chosen interval.
    controls: Optional[list[str]] = None


@dataclass
class SettingSection:
    title: str
    description: str
    items: list[SettingItem]


SETTINGS_SCHEMA: list[SettingSection] = [
    SettingSection(
        title="Server launch parameters",
        description="Flags passed to WSServer.exe on every server start. "
                    "Per-instance ports and server names live in the "
                    "wizard -- editing those mid-deploy invalidates "
                    "firewall rules and save-data references.",
        items=[
            SettingItem(
                key="server.max_players",
                label="Max players (per instance)",
                kind="int", default=10, min=1, max=100,
                help="Becomes -MaxPlayers=N. Applies per server "
                     "instance, so a cluster of two servers with "
                     "max_players=10 caps at 10 players per map. "
                     "Takes effect on next server start.",
            ),
            SettingItem(
                key="server.admin_password",
                label="Admin password",
                kind="secret", default="",
                help="Becomes -adminpsw=\"...\". Required for the "
                     "in-game GM panel. No spaces or quotes. Takes "
                     "effect on next server start.",
            ),
            SettingItem(
                key="server.join_password",
                label="Join password",
                kind="secret", default="",
                help="Becomes -PSW=\"...\". Players need this to "
                     "connect. Leave blank for an open server. No "
                     "spaces or quotes. Takes effect on next server "
                     "start.",
            ),
            SettingItem(
                key="server.game_mode",
                label="Game mode",
                kind="choice", default="pve",
                choices=["pve", "pvp"],
                help="Becomes -pve or -pvp. PvE prevents player-vs-"
                     "player damage; PvP enables it. Switching modes "
                     "mid-world can leave existing structures in odd "
                     "states. Takes effect on next server start.",
            ),
        ],
    ),
    SettingSection(
        title="Web UI",
        description="How the dashboard refreshes and how the UI looks.",
        items=[
            SettingItem(
                key="ui.theme",
                label="Color theme",
                kind="choice", default="dark",
                # DaisyUI v5 ships dozens of built-in themes; this curated
                # subset is the ones that suit a server-admin tool (dark
                # backgrounds, low-key accents). Operators wanting a
                # different one can edit settings.toml directly with any
                # name from https://daisyui.com/docs/themes/ .
                choices=["dark", "dim", "night", "business",
                         "forest", "coffee", "luxury", "synthwave",
                         "halloween", "black", "sunset"],
                help="Sets <html data-theme=\"...\">; DaisyUI components "
                     "(tabs, dropdowns, modals) re-skin to the selected "
                     "palette. Existing utility classes (bg-slate-800 etc.) "
                     "are unaffected. Live -- takes effect on next page "
                     "reload.",
            ),
            SettingItem(
                key="ui.dashboard_refresh_sec",
                label="Dashboard refresh interval (seconds)",
                kind="int", default=5, min=2, max=60,
                help="How often the dashboard re-renders the per-instance "
                     "status panel. Lower = more responsive, slightly more "
                     "load. Live -- takes effect on the next page reload.",
            ),
            SettingItem(
                key="ui.translate_chinese_terms",
                label="Translate Chinese / shorthand terms in logs",
                kind="bool", default=True,
                help="Annotate the log viewer with English translations for "
                     "Chinese phrases (death log, knockdown, invasion phases, "
                     "etc.) and pinyin asset names (BP_WuQi_DaJian_4 -> "
                     "'Tier 4 Great Sword'). Translations appear in mint-green "
                     "[brackets] beside the original token. Useful when your "
                     "terminal can't render Chinese fonts. Live -- takes "
                     "effect when an SSE log connection reconnects (refresh "
                     "the /logs page).",
            ),
        ],
    ),
    SettingSection(
        title="Operations (start / stop / update+restart)",
        description="Thresholds for slow-op warnings and the safety cap. "
                    "The manager NEVER kills processes when caps expire -- "
                    "it just stops monitoring and surfaces a warning.",
        items=[
            SettingItem(
                key="operations.slow_threshold_min",
                label="Slow op threshold (minutes)",
                kind="int", default=5, min=1, max=120,
                help="Op duration after which manager.log heartbeats "
                     "escalate from INFO to WARNING. Not shown in the UI.",
            ),
            SettingItem(
                key="operations.very_slow_threshold_min",
                label="Very slow op threshold (minutes)",
                kind="int", default=15, min=2, max=120,
                help="Above this duration the dashboard shows a red "
                     "'unusually slow' warning + explainer card. Logs "
                     "escalate to ERROR tone.",
            ),
            SettingItem(
                key="operations.hard_cap_min",
                label="Op hard cap (minutes)",
                kind="int", default=30, min=5, max=720,
                help="Maximum time the manager monitors any single op. "
                     "When this expires the manager stops watching and "
                     "logs an ERROR. Processes are NOT killed -- world "
                     "data preservation comes first. Re-trigger the op "
                     "to resume.",
            ),
            SettingItem(
                key="operations.default_stop_countdown_sec",
                label="Default stop countdown (seconds)",
                kind="int", default=30, min=1, max=3600,
                help="Pre-selected value in the Stop dropdown. The "
                     "operator can override per-stop. Empty servers "
                     "always shut down immediately regardless.",
            ),
        ],
    ),
    SettingSection(
        title="Steam update detection",
        description="Background polling for new Soulmask builds.",
        items=[
            SettingItem(
                key="updates.poll_interval_min",
                label="Poll interval (minutes)",
                kind="int", default=10, min=5, max=240,
                restart_required=True,
                help="How often the manager queries Steam for the latest "
                     "build ID. Don't go below 5 -- Valve may rate-limit. "
                     "Restart required for changes to take effect.",
            ),
        ],
    ),
    SettingSection(
        title="Manager update detection",
        description="Background polling for new manager code on the git "
                    "remote. Mirror of the Steam build-id poller; uses "
                    "the deploy key configured on /updates.",
        items=[
            SettingItem(
                key="manager_updates.poll_enabled",
                label="Auto-check for manager updates",
                kind="bool", default=True,
                controls=["manager_updates.poll_interval_min"],
                help="Periodically run `git fetch origin main` so the "
                     "dashboard card shows commits-behind without you "
                     "having to visit /updates manually.",
            ),
            SettingItem(
                key="manager_updates.poll_interval_min",
                label="Poll interval (minutes)",
                kind="int", default=60, min=5, max=1440,
                help="How often to git-fetch. Default 60 min. git fetch "
                     "is cheap but manager updates land at human pace, "
                     "so polling every minute would just be noise.",
            ),
        ],
    ),
    SettingSection(
        title="Backups",
        description="World-database snapshot scheduling. Triggers `bk` over "
                    "EchoPort, integrity-checks, gzips, and indexes the "
                    "result. See docs/BACKUPS.md for full design.",
        items=[
            SettingItem(
                key="backups.schedule_online_enabled",
                label="Auto-backup while players online",
                kind="bool", default=True,
                controls=["backups.schedule_online_interval_hours"],
                help="When at least one player is connected, take a "
                     "scheduled snapshot at the interval below.",
            ),
            SettingItem(
                key="backups.schedule_online_interval_hours",
                label="Online interval (hours)",
                kind="int", default=2, min=1, max=24,
                help="How often to back up while players are online.",
            ),
            SettingItem(
                key="backups.post_logoff_save_enabled",
                label="Backup after last player logs off",
                kind="bool", default=True,
                controls=["backups.post_logoff_delay_minutes"],
                help="One-shot snapshot fires N minutes after the server "
                     "transitions from populated to empty.",
            ),
            SettingItem(
                key="backups.post_logoff_delay_minutes",
                label="Post-logoff delay (minutes)",
                kind="int", default=45, min=1, max=720,
                help="How long to wait after the last player disconnects "
                     "before firing the one-shot save.",
            ),
            SettingItem(
                key="backups.schedule_offline_enabled",
                label="Auto-backup while server is empty",
                kind="bool", default=False,
                controls=["backups.schedule_offline_interval_hours"],
                help="Continue backing up at the offline interval below "
                     "even when no players are connected.",
            ),
            SettingItem(
                key="backups.schedule_offline_interval_hours",
                label="Offline interval (hours)",
                kind="int", default=6, min=1, max=72,
                help="How often to back up while empty (only if the option "
                     "above is on).",
            ),
            SettingItem(
                key="backups.pre_shutdown_backup_enabled",
                label="Backup before user-initiated shutdown",
                kind="bool", default=True,
                help="Take a snapshot just before SaveAndExit is issued "
                     "so the operator has a fresh rollback target. "
                     "Pre-update backups are always on (cannot disable).",
            ),
            SettingItem(
                key="backups.keep_last_n",
                label="Rotation: keep last N",
                kind="int", default=48, min=4, max=500,
                help="Per-instance retention. Pinned snapshots are exempt "
                     "and don't count against this limit.",
            ),
            SettingItem(
                key="backups.disk_free_warn_gb",
                label="Disk-free abort threshold (GB)",
                kind="int", default=2, min=1, max=500,
                help="Snapshots are aborted (with a logged warning) if "
                     "free disk on the manager volume drops below this.",
            ),
            SettingItem(
                key="backups.broadcast_warnings_enabled",
                label="In-game warnings before scheduled backups",
                kind="bool", default=True,
                help="Send a `say` broadcast at T-3 min and T-30 sec "
                     "before each scheduled snapshot. Manual snapshots "
                     "(operator-driven) are silent.",
            ),
        ],
    ),
    SettingSection(
        title="Discord webhook relay",
        description="Forward selected game events to a Discord channel. "
                    "Uses an incoming webhook (per-channel URL, no bot "
                    "token required). Generate a webhook in Discord: "
                    "Channel settings -> Integrations -> Webhooks -> New.",
        items=[
            SettingItem(
                key="discord.relay_enabled",
                label="Enable Discord relay",
                kind="bool", default=False,
                controls=["discord.webhook_url",
                          "discord.batch_interval_sec",
                          "discord.relay_joins_leaves",
                          "discord.send_player_join_map",
                          "discord.relay_player_deaths",
                          "discord.relay_thrall_captures"],
                restart_required=True,
                help="Master switch. When off, no events are sent to "
                     "Discord regardless of the per-category toggles. "
                     "Restart required to start / stop the worker thread.",
            ),
            SettingItem(
                key="discord.webhook_url",
                label="Webhook URL",
                kind="secret", default="",
                help="The full https://discord.com/api/webhooks/... URL "
                     "for the channel you want events posted into. Stored "
                     "redacted in logs. Leave blank on save to keep the "
                     "existing value.",
            ),
            SettingItem(
                key="discord.batch_interval_sec",
                label="Batch interval (seconds)",
                kind="int", default=10, min=0, max=300,
                help="How long to coalesce events before posting. 0 = "
                     "post each event immediately (chatty on busy "
                     "servers; can hit Discord rate limits). Default 10 "
                     "= one combined post every 10 sec, per category.",
            ),
            SettingItem(
                key="discord.relay_joins_leaves",
                label="Player joins / leaves",
                kind="bool", default=True,
                controls=["discord.send_player_join_map"],
                help="Post a one-line message when a player connects or "
                     "disconnects. Format: '+ Player joined the server' / "
                     "'- Player left the server'. Required for the map-"
                     "image sub-option below.",
            ),
            SettingItem(
                key="discord.send_player_join_map",
                label="Attach map image to join messages",
                kind="bool", default=False,
                help="When a player connects, attach a small map "
                     "snapshot to the join message showing where they "
                     "spawned. Single Discord message per join (text + "
                     "image), no extra notification. Image is generated "
                     "by stitching tiles from data/map/<level>/ around "
                     "the player's coords. First-time joiners (no "
                     "journal history) get a whole-map overview "
                     "instead. Requires the joins/leaves option above.",
            ),
            SettingItem(
                key="discord.relay_player_deaths",
                label="Player deaths",
                kind="bool", default=True,
                help="Notify when a player goes down (PvE or PvP). "
                     "WS.log doesn't carry kill attribution -- the "
                     "message is just 'Player died', no killer info. "
                     "Pending HookLogger upgrade for richer detail.",
            ),
            SettingItem(
                key="discord.relay_thrall_captures",
                label="Thrall captures",
                kind="bool", default=True,
                help="Notify when a player recruits a new thrall.",
            ),
        ],
    ),
    SettingSection(
        title="Logging",
        description="manager.log verbosity and rotation.",
        items=[
            SettingItem(
                key="logging.level",
                label="Log level",
                kind="choice", default="DEBUG",
                choices=["VERBOSE", "DEBUG", "INFO", "WARNING", "ERROR"],
                restart_required=True,
                help="VERBOSE = fault hunting (every branch + every value, "
                     "very noisy). DEBUG = build / testing (detailed but "
                     "reasonable). INFO = normal operation (recommended for "
                     "production). WARNING / ERROR = quietest. Restart "
                     "required for the change to take effect.",
            ),
        ],
    ),
]


def all_items() -> list[SettingItem]:
    """Flat list of every SettingItem across all sections."""
    return [item for sec in SETTINGS_SCHEMA for item in sec.items]


def find_item(key: str) -> Optional[SettingItem]:
    for it in all_items():
        if it.key == key:
            return it
    return None
