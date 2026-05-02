"""Setup-wizard data model: parses form input, validates, and serializes
back to settings.toml. The wizard route consumes this; the actual install
(SteamCMD, start scripts, GameXishu copy) lives in `manager.install`.

Data model: per-map config (always two — CloudMist and Shifting Sands).
A separate `main_map` field decides which is the main node in cluster mode
(or which one runs in single mode). Roles / serverids are derived at use
time, not stored.
"""

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# Map id -> friendly name. The two map IDs are fixed by the game.
MAP_CLOUDMIST = "Level01_Main"
MAP_SHIFTINGSANDS = "DLC_Level01_Main"
MAPS = {
    MAP_CLOUDMIST: "Cloud Mist Forest",
    MAP_SHIFTINGSANDS: "Shifting Sands",
}

# Curated subset of the ~90 GameXishu templates the game ships with.
CURATED_TEMPLATES = [
    ("GameXishu_Template.json",          "Default (vanilla)"),
    ("GameXishu_Template_Custom.json",   "Custom (recommended starting point)"),
    ("GameXishu_Template_Jiandan.json",  "Easy"),
    ("GameXishu_Template_Putong.json",   "Normal"),
    ("GameXishu_Template_Kunnan.json",   "Hard"),
    ("GameXishu_Template_Dashi.json",    "Master"),
    ("GameXishu_Template_Xiuxian.json",  "Casual"),
    ("GameXishu_Template_PvE.json",      "PvE base"),
    ("GameXishu_Template_PvP.json",      "PvP base"),
]

# Per-map defaults (ports, default name).
MAP_DEFAULTS = {
    MAP_CLOUDMIST: dict(name="CloudMist", game_port=8777, query_port=27015, echo_port=18888),
    MAP_SHIFTINGSANDS: dict(name="ShiftingSands", game_port=8787, query_port=27017, echo_port=18887),
}
DEFAULT_MAINSERVERPORT = 20000
DEFAULT_MAX_PLAYERS = 50
DEFAULT_TEMPLATE = "GameXishu_Template_Custom.json"


@dataclass
class InstanceConfig:
    """Per-map settings. The role (main/child/single) and serverid are not
    stored here — they're derived from WizardConfig.main_map + mode at
    use-time, so swapping which map is main doesn't shuffle these fields.
    """
    name: str
    map_name: str
    game_port: int
    query_port: int
    echo_port: int


@dataclass
class WizardConfig:
    mode: str = "cluster"          # "single" | "cluster"
    main_map: str = MAP_CLOUDMIST  # which map is "main" (cluster) or active (single)
    admin_password: str = ""
    join_password: str = ""
    game_mode: str = "pve"         # "pve" | "pvp"
    gameplay_template: str = DEFAULT_TEMPLATE
    mainserverport: int = DEFAULT_MAINSERVERPORT
    max_players: int = DEFAULT_MAX_PLAYERS
    instances: list[InstanceConfig] = field(default_factory=list)  # always 2

    @classmethod
    def with_defaults(cls) -> "WizardConfig":
        return cls(
            instances=[
                InstanceConfig(map_name=MAP_CLOUDMIST,     **MAP_DEFAULTS[MAP_CLOUDMIST]),
                InstanceConfig(map_name=MAP_SHIFTINGSANDS, **MAP_DEFAULTS[MAP_SHIFTINGSANDS]),
            ],
        )

    def for_map(self, map_name: str) -> InstanceConfig:
        """Look up the instance config for a given map. Always succeeds for
        well-formed configs (every WizardConfig has both maps populated)."""
        for i in self.instances:
            if i.map_name == map_name:
                return i
        raise KeyError(f"No instance for map {map_name!r}")


@dataclass
class RuntimeInstance:
    """An instance that will actually launch, with role and serverid resolved.
    Built from WizardConfig at the point of use (preview, process control).
    Not stored.
    """
    instance: InstanceConfig
    role: str       # "single" | "main" | "child"
    serverid: int


def active_runtime_instances(config: WizardConfig) -> list[RuntimeInstance]:
    """The instance(s) that will actually be launched, in startup order."""
    main_inst = config.for_map(config.main_map)
    if config.mode == "single":
        return [RuntimeInstance(instance=main_inst, role="single", serverid=1)]

    # Cluster: main first (start order), then child
    other_map = MAP_SHIFTINGSANDS if config.main_map == MAP_CLOUDMIST else MAP_CLOUDMIST
    child_inst = config.for_map(other_map)
    return [
        RuntimeInstance(instance=main_inst,  role="main",  serverid=1),
        RuntimeInstance(instance=child_inst, role="child", serverid=2),
    ]


# ── Parsing form input ──────────────────────────────────────────────────────


def _int_field(form, key, default, errors, display_name, min_v=1, max_v=65535) -> int:
    raw = (form.get(key) or "").strip()
    if not raw:
        log.debug("    field %s: empty, using default=%d", key, default)
        return default
    try:
        v = int(raw)
    except ValueError:
        log.debug("    field %s: not an int (%r), using default=%d", key, raw, default)
        errors.append(f"{display_name} must be a number (got {raw!r}).")
        return default
    if not (min_v <= v <= max_v):
        log.debug("    field %s: %d out of range [%d,%d]", key, v, min_v, max_v)
        errors.append(f"{display_name} must be between {min_v} and {max_v} (got {v}).")
    else:
        log.debug("    field %s: %d", key, v)
    return v


def _parse_map_section(form, prefix: str, map_name: str, errors: list[str]) -> InstanceConfig:
    label = MAPS[map_name]
    defaults = MAP_DEFAULTS[map_name]
    log.debug("  Parsing map section %s (prefix=%s, defaults=%s)", label, prefix, defaults)

    name = (form.get(f"{prefix}_name") or "").strip()
    if not name:
        log.debug("    field %s_name: empty (will fail validation)", prefix)
        errors.append(f"{label}: server name is required.")
    elif '"' in name:
        log.debug("    field %s_name: has invalid character (double quote)", prefix)
        errors.append(f"{label}: server name cannot contain double quotes.")
    else:
        log.debug("    field %s_name: %r", prefix, name)

    inst = InstanceConfig(
        name=name,
        map_name=map_name,
        game_port=_int_field(form, f"{prefix}_game_port", defaults["game_port"],
                             errors, f"{label} game port"),
        query_port=_int_field(form, f"{prefix}_query_port", defaults["query_port"],
                              errors, f"{label} query port"),
        echo_port=_int_field(form, f"{prefix}_echo_port", defaults["echo_port"],
                             errors, f"{label} EchoPort"),
    )
    log.debug("  -> %s instance: name=%r ports=g%d/q%d/e%d", label,
              inst.name, inst.game_port, inst.query_port, inst.echo_port)
    return inst


def parse_form(form, manager_bind_port: int = 5000) -> tuple[WizardConfig, list[str]]:
    """Parse form data into WizardConfig + list of validation error messages.
    Returns the (possibly-invalid) config so the form can re-render with the
    user's input intact alongside the errors.
    """
    log.debug("parse_form: starting, %d form fields received", len(form))
    errors: list[str] = []

    # ── Mode ──
    mode = (form.get("mode") or "").strip().lower()
    log.debug("  mode=%r", mode)
    if mode not in ("single", "cluster"):
        errors.append("Mode must be 'single' or 'cluster'.")
        log.debug("  mode invalid -> defaulting to 'cluster'")
        mode = "cluster"

    # ── Map selection ──
    if mode == "single":
        main_map = (form.get("single_map") or MAP_CLOUDMIST).strip()
        log.debug("  single_map=%r", main_map)
    else:
        main_map = (form.get("cluster_main_map") or MAP_CLOUDMIST).strip()
        log.debug("  cluster_main_map=%r", main_map)
    if main_map not in MAPS:
        errors.append(f"Invalid map selection: {main_map!r}.")
        log.debug("  main_map invalid -> defaulting to %s", MAP_CLOUDMIST)
        main_map = MAP_CLOUDMIST

    # ── Per-map sections (always parse both, even in single mode -- we keep
    #    both stored so toggling modes doesn't lose tweaks) ──
    instances = [
        _parse_map_section(form, "cm", MAP_CLOUDMIST, errors),
        _parse_map_section(form, "ss", MAP_SHIFTINGSANDS, errors),
    ]

    # ── Auth ──
    admin_password = (form.get("admin_password") or "").strip()
    log.debug("  admin_password: %s (len=%d)",
              "set" if admin_password else "empty", len(admin_password))
    if not admin_password:
        errors.append("Admin password is required.")
    elif " " in admin_password or '"' in admin_password:
        errors.append("Admin password cannot contain spaces or double quotes "
                      "(Soulmask launch params can't quote-escape them).")

    join_password = (form.get("join_password") or "").strip()
    log.debug("  join_password: %s (len=%d)",
              "set (server is locked)" if join_password else "empty (open server)",
              len(join_password))
    if join_password and (" " in join_password or '"' in join_password):
        errors.append("Join password cannot contain spaces or double quotes.")

    # ── Game mode ──
    game_mode = (form.get("game_mode") or "pve").strip().lower()
    log.debug("  game_mode=%r", game_mode)
    if game_mode not in ("pve", "pvp"):
        errors.append("Game mode must be PvE or PvP.")
        game_mode = "pve"

    # ── Template ──
    gameplay_template = (form.get("gameplay_template") or "").strip()
    log.debug("  gameplay_template=%r", gameplay_template)
    valid_templates = {t[0] for t in CURATED_TEMPLATES}
    if gameplay_template not in valid_templates:
        errors.append(f"Unknown gameplay tuning preset: {gameplay_template!r}.")
        gameplay_template = DEFAULT_TEMPLATE

    # ── Shared numerics ──
    max_players = _int_field(form, "max_players", DEFAULT_MAX_PLAYERS,
                             errors, "Max players", min_v=1, max_v=999)
    mainserverport = _int_field(form, "mainserverport", DEFAULT_MAINSERVERPORT,
                                errors, "Cluster link port")

    # ── Port collision check (only for ports that will actually be used) ──
    used_ports: list[int] = []
    if mode == "single":
        active = next(i for i in instances if i.map_name == main_map)
        used_ports.extend([active.game_port, active.query_port, active.echo_port])
    else:
        for i in instances:
            used_ports.extend([i.game_port, i.query_port, i.echo_port])
        used_ports.append(mainserverport)
    if manager_bind_port in used_ports:
        errors.append(f"Port {manager_bind_port} is in use by the manager UI itself "
                      "(see settings.toml [network].bind_port).")
    seen: dict[int, int] = {}
    for p in used_ports:
        seen[p] = seen.get(p, 0) + 1
    duplicates = sorted([p for p, n in seen.items() if n > 1])
    if duplicates:
        log.debug("  port collision detected: %s (used: %s)", duplicates, used_ports)
        errors.append(f"Port collision -- each instance needs unique ports. "
                      f"Conflicting: {duplicates}")
    else:
        log.debug("  port collision check: OK (used ports: %s)", used_ports)

    config = WizardConfig(
        mode=mode,
        main_map=main_map,
        admin_password=admin_password,
        join_password=join_password,
        game_mode=game_mode,
        gameplay_template=gameplay_template,
        mainserverport=mainserverport,
        max_players=max_players,
        instances=instances,
    )
    log.debug("parse_form: complete. mode=%s main_map=%s max_players=%d errors=%d",
              config.mode, config.main_map, config.max_players, len(errors))
    return config, errors


# ── Serialize / load round-trip with settings.toml ───────────────────────────


def to_settings_dict(config: WizardConfig, base_settings: dict) -> dict:
    """Merge wizard config into the base settings dict, preserving non-server
    sections ([paths], [network], [logging], etc.)."""
    new = dict(base_settings)
    new["server"] = {
        "mode": config.mode,
        "main_map": config.main_map,
        "admin_password": config.admin_password,
        "join_password": config.join_password,
        "game_mode": config.game_mode,
        "gameplay_template": config.gameplay_template,
        "mainserverport": config.mainserverport,
        "max_players": config.max_players,
        "instances": [
            {
                "name": i.name,
                "map": i.map_name,
                "game_port": i.game_port,
                "query_port": i.query_port,
                "echo_port": i.echo_port,
            }
            for i in config.instances
        ],
    }
    return new


def load_existing(settings: dict) -> WizardConfig:
    """Load wizard config from settings dict. Tolerates the older schema
    (per-instance role/serverid/max_players) for graceful migration.

    NOTE: this is called on every status poll. We deliberately do NOT log
    routine load steps -- they'd drown the firehose. Migration paths and
    skip-of-unknown-data still log because those indicate something
    unexpected.
    """
    if "server" not in settings:
        return WizardConfig.with_defaults()

    s = settings["server"]
    raw_instances = s.get("instances", [])

    # Collect saved instance data keyed by map name.
    by_map: dict[str, InstanceConfig] = {}
    for i in raw_instances:
        m = i.get("map", MAP_CLOUDMIST)
        if m not in MAPS:
            log.warning("load_existing: skipping unknown map %r in saved instances", m)
            continue
        defaults = MAP_DEFAULTS[m]
        by_map[m] = InstanceConfig(
            name=i.get("name", "") or defaults["name"],
            map_name=m,
            game_port=i.get("game_port", defaults["game_port"]),
            query_port=i.get("query_port", defaults["query_port"]),
            echo_port=i.get("echo_port", defaults["echo_port"]),
        )

    # Fill any missing maps with defaults so the form always has both.
    for m in (MAP_CLOUDMIST, MAP_SHIFTINGSANDS):
        if m not in by_map:
            log.info("load_existing: %s missing from saved data, using defaults", m)
            by_map[m] = InstanceConfig(map_name=m, **MAP_DEFAULTS[m])

    instances = [by_map[MAP_CLOUDMIST], by_map[MAP_SHIFTINGSANDS]]

    # Determine main_map: explicit field, else derive from old role attr, else default.
    main_map = s.get("main_map")
    if not main_map or main_map not in MAPS:
        for i in raw_instances:
            if i.get("role") == "main" and i.get("map") in MAPS:
                main_map = i["map"]
                log.info("load_existing: derived main_map=%s from legacy role=main entry",
                         main_map)
                break
        else:
            log.info("load_existing: no main_map and no legacy role=main; defaulting to %s",
                     MAP_CLOUDMIST)
            main_map = MAP_CLOUDMIST

    # max_players: top-level if present, else from the first instance, else default.
    max_players = s.get("max_players")
    if max_players is None:
        log.info("load_existing: max_players missing at top level; "
                 "falling back to legacy per-instance value")
        max_players = (raw_instances[0].get("max_players", DEFAULT_MAX_PLAYERS)
                       if raw_instances else DEFAULT_MAX_PLAYERS)

    return WizardConfig(
        mode=s.get("mode", "cluster"),
        main_map=main_map,
        admin_password=s.get("admin_password", ""),
        join_password=s.get("join_password", ""),
        game_mode=s.get("game_mode", "pve"),
        # accept both new and old key names for the template field
        gameplay_template=s.get("gameplay_template",
                                s.get("gamexishu_template", DEFAULT_TEMPLATE)),
        mainserverport=s.get("mainserverport", DEFAULT_MAINSERVERPORT),
        max_players=max_players,
        instances=instances,
    )


# ── Launch arg derivation (for preview + lifecycle.start_one) ───────────────


def build_launch_args(config: WizardConfig, ri: RuntimeInstance) -> list[str]:
    """Compose the WSServer.exe launch arg list for one runtime instance."""
    inst = ri.instance
    log.debug("build_launch_args: map=%s role=%s serverid=%d",
              inst.map_name, ri.role, ri.serverid)
    args = [
        inst.map_name,
        "-server", "-log", "-UTF8Output", "-forcepassthrough",
        "-MULTIHOME=0.0.0.0",
        f"-PORT={inst.game_port}",
        f"-QueryPort={inst.query_port}",
        f"-EchoPort={inst.echo_port}",
        f'-SteamServerName="{inst.name}"',
        f"-MaxPlayers={config.max_players}",
        f'-adminpsw="{config.admin_password}"',
    ]
    if config.join_password:
        args.append(f'-PSW="{config.join_password}"')
    args.append(f"-{config.game_mode}")  # -pve or -pvp

    # -mod="<workshop_id_csv>" advertises mod IDs to connecting clients
    # so they're prompted to subscribe via Steam. Server-side pak mounting
    # auto-discovers everything in WS\Mods\ regardless. Populated from
    # data/mods_subscribed.json by the /mods page. Singular -mod=, NOT
    # -mods=. See memory/soulmask_mod_flag.md.
    try:
        from manager import mods as _mods
        wsids = _mods.subscribed_workshop_ids()
        if wsids:
            args.append(f'-mod="{",".join(wsids)}"')
    except Exception:
        log.exception("build_launch_args: reading mod manifest failed "
                      "(non-fatal, launching without -mod=)")

    if config.mode == "cluster":
        args.append(f"-serverid={ri.serverid}")
        if ri.role == "main":
            args.append(f"-mainserverport={config.mainserverport}")
            log.debug("  cluster main: -mainserverport=%d", config.mainserverport)
        elif ri.role == "child":
            args.append(f"-clientserverconnect=127.0.0.1:{config.mainserverport}")
            log.debug("  cluster child: -clientserverconnect=127.0.0.1:%d",
                      config.mainserverport)

    log.debug("  built %d launch args (passwords are present in the list and will be"
              " redacted in log output by the formatter)", len(args))
    return args
