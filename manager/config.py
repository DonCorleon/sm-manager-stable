"""Settings lifecycle. Reads/writes data/settings.toml."""

import logging
import textwrap
import tomllib
from pathlib import Path
from threading import RLock

# tomli_w is only needed by save_settings() (the POST /settings/ path).
# Reading settings.toml uses stdlib's `tomllib`. Deferring the import
# means modules that only need DATA_DIR / load_settings() / get_setting()
# (e.g. tools/scraper.py running outside the manager's venv) can still
# import this module without the optional write-side dependency.

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
DATA_DIR = PROJECT_ROOT / "data"
SETTINGS_PATH = DATA_DIR / "settings.toml"

log = logging.getLogger(__name__)
_lock = RLock()


def resolve_path(value: str | Path) -> Path:
    """Resolve a path setting. Absolute paths returned as-is; relative paths
    resolved against the manager's project root (so settings.toml stays
    portable — copy SM_Manager elsewhere and the relative paths still work).
    """
    p = Path(value)
    if p.is_absolute():
        return p.resolve()
    return (PROJECT_ROOT / p).resolve()


# Substrings in dict keys that mark the value as a secret. Used by redact_dict
# to scrub form data / settings dumps before they hit logs at DEBUG level.
SECRET_KEY_FRAGMENTS = ("password", "psw", "webhook", "secret", "token")


def redact_dict(d) -> dict:
    """Return a shallow copy of dict d with values masked for any key whose
    name contains a known secret fragment. Use when logging form payloads or
    raw settings dicts at DEBUG level so we never accidentally write a real
    password to a log file.

    >>> redact_dict({'admin_password': 'foo', 'mode': 'cluster'})
    {'admin_password': '***', 'mode': 'cluster'}
    """
    out = {}
    for k, v in d.items():
        kl = str(k).lower()
        if any(frag in kl for frag in SECRET_KEY_FRAGMENTS):
            out[k] = "***"
        else:
            out[k] = v
    return out


def display_path(p: Path | str) -> str:
    """Format a path for human reading — relative to the manager's project
    root when that's both meaningful AND shorter than absolute. Used in log
    lines and UI to avoid leaking the full system layout.

    Examples (with PROJECT_ROOT = D:\\Soulmask\\SM_Manager):
      D:\\Soulmask\\WSServer.exe                       -> ..\\WSServer.exe
      D:\\Soulmask\\SM_Manager\\steamcmd\\steamcmd.exe -> steamcmd\\steamcmd.exe
      D:\\Soulmask\\                                   -> D:\\Soulmask  (abs — pure ".." is unhelpful)
      C:\\elsewhere\\thing                             -> C:\\elsewhere\\thing  (different drive)
    """
    import os
    p = Path(p)
    try:
        rel = os.path.relpath(p, PROJECT_ROOT)
    except ValueError:
        return str(p)  # different drive on Windows
    # Pure "." or ".." carry no information about WHAT the path points to.
    # Same for absurdly-deep parent traversals.
    if rel in (".", "..") or rel.startswith("..\\..\\..\\") or rel.startswith("../../../"):
        return str(p)
    return rel


# Defaults below are kept relative on purpose. Resolved at read time via
# resolve_path(). User can edit settings.toml to use absolute paths if they
# want the install or steamcmd elsewhere.
_DEFAULT_SETTINGS = textwrap.dedent("""\
    # Soulmask Manager settings
    # Edited via the web UI; manual edits picked up on restart.
    #
    # Path values support both relative (to this SM_Manager dir) and absolute
    # forms. Relative is preferred for portability.

    [paths]
    # Soulmask install root (parent of the WS/ directory).
    # Default ".." = the dir that contains SM_Manager.
    install_dir = ".."
    # SteamCMD lives inside the manager dir to stay self-contained.
    # The setup wizard will download and extract it here if missing.
    steamcmd_exe = "steamcmd/steamcmd.exe"

    [network]
    bind_host = "0.0.0.0"
    bind_port = 5000

    [logging]
    # Root level for manager.log (DEBUG / INFO / WARNING / ERROR).
    # DEBUG is the build-phase default and is very noisy by design.
    # Set to INFO once things are stable.
    level = "DEBUG"
    """)


def _default_settings_text() -> str:
    return _DEFAULT_SETTINGS


def ensure_settings() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_PATH.exists():
        log.info("settings.toml not found; writing defaults to %s", SETTINGS_PATH)
        SETTINGS_PATH.write_text(_default_settings_text(), encoding="utf-8")
        log.info("settings.toml created. Edit it or use the wizard to change defaults.")
    # No log line in the routine "exists" branch -- it fires on every poll and
    # is useless noise.


def load_settings() -> dict:
    # Routine successful loads are not logged. They happen on every status
    # poll (every 5s by default), so any DEBUG line here is firehose noise
    # that drowns out actual events. ensure_settings() still logs the rare
    # "creating defaults" path.
    with _lock:
        ensure_settings()
        with SETTINGS_PATH.open("rb") as f:
            return tomllib.load(f)


_SETTINGS_HEADER = (
    b"# Soulmask Manager settings\n"
    b"# Path values support both relative (preferred -- portable) and absolute forms.\n"
    b"# Edited via the web UI; manual edits picked up on restart.\n"
    b"#\n"
    b"# This file is regenerated by the setup wizard. Comments below are not preserved\n"
    b"# on programmatic save (header above always is).\n\n"
)


def get_setting(key: str, default=None):
    """Read a dotted-path setting (e.g. 'operations.slow_threshold_min').
    Returns `default` if any segment of the path is missing.

    Cheap to call -- load_settings() reads a small TOML file -- but if a
    hot path needs many lookups, batch them or load once and use .get().
    """
    s = load_settings()
    cursor = s
    for part in key.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor


def set_setting(key: str, value) -> None:
    """Update a dotted-path setting and atomically save settings.toml.
    Creates intermediate dicts if missing. Other top-level sections are
    preserved untouched."""
    with _lock:
        s = load_settings()
        cursor = s
        parts = key.split(".")
        for part in parts[:-1]:
            if part not in cursor or not isinstance(cursor[part], dict):
                cursor[part] = {}
            cursor = cursor[part]
        cursor[parts[-1]] = value
        save_settings(s)


def save_settings(data: dict) -> None:
    """Atomically write the settings dict to settings.toml.

    Preserves a fixed header comment block. Per-key inline comments are not
    preserved (tomli_w doesn't round-trip them). Edit defaults in
    `_DEFAULT_SETTINGS` if you want them in fresh installs.
    """
    # tomli_w only required by writers (this function). Imported here so
    # readers don't pay for it. The manager's venv has tomli_w installed
    # via requirements.txt; ad-hoc operator tools running outside the
    # venv (tools/scraper.py) can still import this module.
    import tomli_w
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        log.debug("Saving settings: top-level keys=%s", sorted(data.keys()))
        if "server" in data:
            log.debug("  [server] keys=%s", sorted(data["server"].keys()))
        body = tomli_w.dumps(data).encode("utf-8")
        tmp = SETTINGS_PATH.parent / (SETTINGS_PATH.name + ".tmp")
        tmp.write_bytes(_SETTINGS_HEADER + body)
        tmp.replace(SETTINGS_PATH)
        log.info("Settings saved to %s (%d bytes)", SETTINGS_PATH, len(body))
