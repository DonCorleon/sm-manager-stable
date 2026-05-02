"""Canonical filesystem paths derived from settings.toml.

Functions here are pure: they compute paths from current settings, but do not
read or write files themselves. The bootstrap checks ask whether each path
exists; the wizard later writes to them.

All path settings are resolved through manager.config.resolve_path() so both
relative (preferred — portable) and absolute settings work.
"""

from pathlib import Path
from typing import Optional

from manager.config import load_settings, resolve_path

# Steam app IDs for Soulmask dedicated server.
SOULMASK_WINDOWS_APP_ID = 3017310
SOULMASK_LINUX_APP_ID = 3017300


def install_dir() -> Path:
    return resolve_path(load_settings()["paths"]["install_dir"])


def steamcmd_exe() -> Path:
    return resolve_path(load_settings()["paths"]["steamcmd_exe"])


def server_exe() -> Path:
    return install_dir() / "WSServer.exe"


def saved_dir() -> Path:
    return install_dir() / "WS" / "Saved"


def gamexishu_path() -> Path:
    return saved_dir() / "GameplaySettings" / "GameXishu.json"


def engine_ini_path() -> Path:
    return saved_dir() / "Config" / "WindowsServer" / "Engine.ini"


def appmanifest_path(app_id: int = SOULMASK_WINDOWS_APP_ID) -> Path:
    return install_dir() / "steamapps" / f"appmanifest_{app_id}.acf"


def world_db_path(map_name: str) -> Path:
    return saved_dir() / "Worlds" / "Dedicated" / map_name / "world.db"


def server_log_path(secondary: bool = False) -> Path:
    name = "WS_2.log" if secondary else "WS.log"
    return saved_dir() / "Logs" / name


def level_for_server(server: str) -> Optional[str]:
    """Map an event/tailer server name ('ws' or 'ws_2') to the Soulmask
    level identifier ('Level01_Main' or 'DLC_Level01_Main').

    Reads the cluster configuration to know which map is 'main' (ws)
    vs 'other' (ws_2). Returns None for unknown server names or when
    the configuration can't be loaded (manager not yet set up).

    Used by Discord image-on-join, future /map page, etc. -- anywhere
    we need to know which map the event came from to load the right
    tile pyramid + POI data."""
    if server not in ("ws", "ws_2"):
        return None
    try:
        from manager.config import load_settings
        from manager.wizard import (
            MAP_CLOUDMIST, MAP_SHIFTINGSANDS, load_existing,
        )
        config = load_existing(load_settings())
    except Exception:
        # Settings not loadable (very early boot, missing settings, etc.)
        return None
    if not config or not config.main_map:
        return None
    if server == "ws":
        return config.main_map
    # server == "ws_2"
    if config.mode == "cluster":
        return (MAP_SHIFTINGSANDS if config.main_map == MAP_CLOUDMIST
                else MAP_CLOUDMIST)
    return None  # ws_2 is unused in single-map mode
