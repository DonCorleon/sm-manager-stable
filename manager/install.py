"""SteamCMD bootstrap + Soulmask install runner.

Runs in a background daemon thread. Single global state (only one install at
a time). State is in-memory: a manager restart loses the UI state but the
underlying SteamCMD operations are idempotent, so re-clicking is safe.

Lifecycle:
  IDLE -> RUNNING (steamcmd download) -> RUNNING (soulmask install)
       -> COMPLETED  or  FAILED
  RUNNING -> RUNNING (re-entry blocked)
  COMPLETED/FAILED -> RUNNING (resets and starts over)
"""

import io
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from manager.config import display_path
from manager.paths import (
    SOULMASK_WINDOWS_APP_ID,
    install_dir,
    server_exe,
    steamcmd_exe,
)

log = logging.getLogger(__name__)

STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"

# Status values used by the UI.
STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# Step names shown in the UI.
STEP_PREP = "Preparing"
STEP_STEAMCMD_DOWNLOAD = "Downloading SteamCMD"
STEP_STEAMCMD_INIT = "Initialising SteamCMD (self-update)"
STEP_GIT = "Installing PortableGit (for self-update)"
STEP_SOULMASK = "Installing Soulmask via SteamCMD"
STEP_VERIFY = "Verifying install"
STEP_CONFIG = "Generating start scripts and config files"
STEP_DONE = "Done"


@dataclass
class _State:
    status: str = STATUS_IDLE
    step: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: str = ""
    # (timestamp, line) tuples; deque drops oldest when at capacity.
    output_lines: deque = field(default_factory=lambda: deque(maxlen=2000))

    def snapshot(self) -> dict:
        """Return a JSON-friendly snapshot for templates."""
        return {
            "status": self.status,
            "step": self.step,
            "started_at": self.started_at.strftime("%Y-%m-%d %H:%M:%S") if self.started_at else "",
            "finished_at": self.finished_at.strftime("%Y-%m-%d %H:%M:%S") if self.finished_at else "",
            "elapsed_seconds": int((self._end_time() - self.started_at).total_seconds())
                               if self.started_at else 0,
            "error": self.error,
            "lines": list(self.output_lines),
            "line_count": len(self.output_lines),
        }

    def _end_time(self) -> datetime:
        return self.finished_at or datetime.now()


_state = _State()
_lock = threading.Lock()

# Subscribers for /setup/install/sse. Each holds a threading.Event;
# producers `_notify()` to wake them on every line append + status
# change. Pattern mirrors dashboard_events / updates_log.
_subscribers: list[threading.Event] = []
_subs_lock = threading.Lock()


def subscribe() -> threading.Event:
    e = threading.Event()
    with _subs_lock:
        _subscribers.append(e)
    return e


def unsubscribe(event: threading.Event) -> None:
    with _subs_lock:
        try:
            _subscribers.remove(event)
        except ValueError:
            pass


def _notify() -> None:
    with _subs_lock:
        subs = list(_subscribers)
    for e in subs:
        e.set()


# ── Public API ──────────────────────────────────────────────────────────────


def get_state() -> dict:
    with _lock:
        return _state.snapshot()


def is_running() -> bool:
    with _lock:
        return _state.status == STATUS_RUNNING


def start() -> bool:
    """Kick off the install in a background thread.
    Returns True if started, False if one is already running."""
    with _lock:
        if _state.status == STATUS_RUNNING:
            log.warning("install.start: already running, ignoring re-entry")
            return False
        log.info("install.start: kicking off background install")
        # Reset in place so consumers holding a reference see fresh values.
        _state.status = STATUS_RUNNING
        _state.step = STEP_PREP
        _state.started_at = datetime.now()
        _state.finished_at = None
        _state.error = ""
        _state.output_lines.clear()
    _notify()
    threading.Thread(target=_run, daemon=True, name="install-runner").start()
    return True


# ── Internal helpers ────────────────────────────────────────────────────────


def _add_line(line: str, level: int = logging.DEBUG) -> None:
    """Append a line to the UI output buffer AND log it. Most subprocess
    output is DEBUG (firehose); explicit status lines come in at INFO."""
    line = line.rstrip()
    if not line:
        return
    with _lock:
        _state.output_lines.append((datetime.now().strftime("%H:%M:%S"), line))
    log.log(level, "[install] %s", line)
    _notify()


def _set_step(step: str) -> None:
    with _lock:
        _state.step = step
    log.info("[install] STEP: %s", step)
    _add_line(f"--- {step} ---", level=logging.INFO)
    _notify()


def _set_status(status: str, error: str = "") -> None:
    with _lock:
        _state.status = status
        _state.finished_at = datetime.now()
        if error:
            _state.error = error
    log.info("[install] STATUS -> %s%s", status, f" ({error})" if error else "")
    _notify()


# ── Thread main ─────────────────────────────────────────────────────────────


def _run() -> None:
    try:
        log.debug("install thread started")

        # Step 1: SteamCMD
        cmd_path = steamcmd_exe()
        if cmd_path.exists():
            _add_line(f"SteamCMD already present at {display_path(cmd_path)}", level=logging.INFO)
        else:
            _set_step(STEP_STEAMCMD_DOWNLOAD)
            _download_steamcmd(cmd_path)
            _set_step(STEP_STEAMCMD_INIT)
            _init_steamcmd(cmd_path)

        # Step 1b: PortableGit. Required for the self-update flow on
        # /manager-updates; idempotent (skips if already present). We
        # do this BEFORE Soulmask install because it's small and fast
        # (~1-2 min) compared to Soulmask (~10-30 min); fail-fast on
        # network issues is better here.
        _set_step(STEP_GIT)
        _install_portable_git()

        # Step 2: Soulmask install
        _set_step(STEP_SOULMASK)
        _install_soulmask(cmd_path)

        # Step 3: Verify
        _set_step(STEP_VERIFY)
        _verify_install()

        # Step 4: Generate start scripts + GameXishu + cross-server patch
        _set_step(STEP_CONFIG)
        _write_config_files()

        _set_step(STEP_DONE)
        _set_status(STATUS_COMPLETED)
    except Exception as e:
        log.exception("install thread crashed")
        _add_line(f"FATAL: {type(e).__name__}: {e}", level=logging.ERROR)
        _set_status(STATUS_FAILED, error=f"{type(e).__name__}: {e}")


def _download_steamcmd(target_exe: Path) -> None:
    target_dir = target_exe.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    log.debug("download_steamcmd: target_dir=%s", target_dir)
    _add_line(f"GET {STEAMCMD_URL}")
    response = requests.get(STEAMCMD_URL, timeout=60, stream=True)
    response.raise_for_status()
    content_length = response.headers.get("content-length", "?")
    _add_line(f"HTTP {response.status_code}, content-length={content_length}")
    data = response.content  # small file, OK to load fully
    _add_line(f"Downloaded {len(data)} bytes; extracting to {display_path(target_dir)}")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        log.debug("steamcmd zip contents: %s", names)
        zf.extractall(target_dir)
    if not target_exe.exists():
        raise RuntimeError(
            f"steamcmd.exe not found at {target_exe} after extract -- "
            f"zip contents were {names}"
        )
    _add_line(f"Extracted. steamcmd.exe at {display_path(target_exe)}", level=logging.INFO)


def _init_steamcmd(steamcmd: Path) -> None:
    """First run: steamcmd downloads its own dependencies and exits.
    Takes 30-120 sec depending on network.

    Known Windows bug: after the self-update, steamcmd.exe often returns rc=7
    even though the update succeeded ('Update complete, launching...' then
    immediate exit). We treat the first run's rc as advisory and verify by
    running it a second time -- THAT one must be clean.
    """
    _add_line("Running first-time SteamCMD self-update (~30-120 sec)...",
              level=logging.INFO)
    rc1 = _run_subprocess([str(steamcmd), "+quit"])
    _add_line(f"First run finished with rc={rc1} "
              f"(rc=7 on Windows is the harmless 'self-update completed' code)",
              level=logging.INFO)

    _add_line("Verifying SteamCMD is operational (second run should be fast and clean)...",
              level=logging.INFO)
    rc2 = _run_subprocess([str(steamcmd), "+quit"])
    if rc2 != 0:
        raise RuntimeError(
            f"SteamCMD verification failed: second run exited with code {rc2} "
            f"(first run rc={rc1}). Likely a network drop, disk full, or AV blocking. "
            f"Check the output above for ERROR lines."
        )
    _add_line("SteamCMD ready.", level=logging.INFO)


def _install_soulmask(steamcmd: Path) -> None:
    target = install_dir()
    target.mkdir(parents=True, exist_ok=True)
    _add_line(f"Install target: {target}", level=logging.INFO)
    _add_line(
        "This is the slow part -- 5 to 15 GB depending on the build, "
        "10 to 30 min on a typical home connection.",
        level=logging.INFO,
    )
    cmd = [
        str(steamcmd),
        "+force_install_dir", str(target),
        "+login", "anonymous",
        "+app_update", str(SOULMASK_WINDOWS_APP_ID), "validate",
        "+quit",
    ]
    _add_line(f"cmd: {' '.join(cmd)}")
    rc = _run_subprocess(cmd)
    if rc != 0:
        raise RuntimeError(
            f"SteamCMD app_update exited with code {rc} -- check the output above "
            f"for 'ERROR' lines (common: disk full, network drop, Steam outage)"
        )


def _verify_install() -> None:
    exe = server_exe()
    if not exe.exists():
        raise RuntimeError(f"WSServer.exe not found at {exe} after install completed")
    size = exe.stat().st_size
    _add_line(f"WSServer.exe present at {display_path(exe)} ({size:,} bytes)",
              level=logging.INFO)


def _install_portable_git() -> None:
    """Wraps install_git.install_portable_git_sync() so its progress
    lines show up in the SteamCMD/Soulmask install output buffer too.
    Skips if git is already resolvable from a system install
    (operator already has Git for Windows on PATH or in a known
    location -- no need to download a second copy).

    Failure is non-fatal here: we log + warn, continue the rest of
    setup. Self-update won't work until git is available, but the
    operator still gets a usable game-server install.
    """
    from manager import install_git, self_update

    # Already have system git? Skip.
    if self_update._resolve_binary("git"):
        _add_line(
            f"git already resolvable at {self_update._resolve_binary('git')}; "
            f"skipping PortableGit download.",
            level=logging.INFO,
        )
        return
    if install_git.is_installed():
        _add_line("PortableGit already extracted; skipping download.",
                  level=logging.INFO)
        # Still bust the cache in case the resolver was queried before
        # the directory existed.
        self_update._BINARY_CACHE.clear()
        return

    _add_line(
        "git not found anywhere; downloading PortableGit (~50MB) so the "
        "manager can self-update. ~1-2 min.",
        level=logging.INFO,
    )
    try:
        install_git.install_portable_git_sync()
        _add_line("PortableGit installed.", level=logging.INFO)
    except Exception as e:
        _add_line(
            f"PortableGit install FAILED: {e}. Self-update via /manager-"
            f"updates won't work until git is available, but the rest of "
            f"setup will continue. Re-run setup later to retry.",
            level=logging.WARNING,
        )


# ── Config file generation (GameXishu + cross-server patch) ──────────────────


def _write_config_files() -> None:
    """Copy the chosen GameXishu template into Saved/, and patch KaiQiKuaFu=1
    if running a cluster. Idempotent -- safe to re-run. Pulls live wizard
    config from settings.toml so this reflects the latest save even if the
    install itself was run earlier with different choices.
    """
    from manager.config import load_settings
    from manager.wizard import load_existing

    config = load_existing(load_settings())
    install_root = install_dir()
    log.debug("config-write: install_root=%s, mode=%s, main_map=%s",
              install_root, config.mode, config.main_map)

    # 1. GameXishu template copy
    _copy_gamexishu(install_root, config.gameplay_template)

    # 2. KaiQiKuaFu patch for cluster (cross-server enable)
    if config.mode == "cluster":
        _patch_cross_server_flag(install_root)
    else:
        _add_line("Single-map mode: skipping KaiQiKuaFu patch (not needed)",
                  level=logging.INFO)


def _copy_gamexishu(install_root: Path, template_filename: str) -> None:
    """Copy the chosen template from WS/Config/GameplaySettings/ to the live
    Saved/GameplaySettings/GameXishu.json. OVERWRITES any existing file --
    re-running setup is treated as 'make my install match the wizard.'"""
    src = install_root / "WS" / "Config" / "GameplaySettings" / template_filename
    dst_dir = install_root / "WS" / "Saved" / "GameplaySettings"
    dst = dst_dir / "GameXishu.json"

    if not src.exists():
        raise RuntimeError(
            f"GameXishu template not found at {display_path(src)} -- "
            f"the install may be incomplete or the template name is wrong"
        )

    dst_dir.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        _add_line(f"Overwriting existing GameXishu.json at {display_path(dst)}",
                  level=logging.INFO)
    else:
        _add_line(f"Writing {display_path(dst)} from template {template_filename}",
                  level=logging.INFO)
    log.debug("  src=%s, dst=%s, template_size=%d bytes",
              src, dst, src.stat().st_size)
    shutil.copy2(src, dst)


def _patch_cross_server_flag(install_root: Path) -> None:
    """Set KaiQiKuaFu=1 in profile "1" of GameXishu.json. Required for
    character transfer between cluster nodes."""
    path = install_root / "WS" / "Saved" / "GameplaySettings" / "GameXishu.json"
    if not path.exists():
        raise RuntimeError(
            f"GameXishu.json not at {display_path(path)} -- can't patch "
            f"KaiQiKuaFu (template copy step should have run first)"
        )

    _add_line("Patching KaiQiKuaFu=1 in GameXishu.json profile \"1\" (cross-server enable)",
              level=logging.INFO)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if "1" not in data:
        raise RuntimeError(
            'GameXishu.json missing profile "1" -- file structure unexpected. '
            'Profiles "0", "1", "2" should be top-level keys.'
        )

    before = data["1"].get("KaiQiKuaFu", "<unset>")
    data["1"]["KaiQiKuaFu"] = 1
    log.debug("  KaiQiKuaFu in profile '1': %s -> 1", before)

    serialized = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    _atomic_write_bytes(path, serialized)
    _add_line(f"GameXishu.json updated ({len(serialized)} bytes)", level=logging.INFO)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write to a sibling .tmp then rename, so partial writes never appear
    on disk under the real path."""
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)


_HEARTBEAT_QUIET_SECS = 30  # log a heartbeat after this many seconds with no output


def _run_subprocess(cmd: list[str]) -> int:
    """Run a command, streaming each stdout/stderr line into the UI buffer.

    Reads RAW BYTES via read1() and splits lines manually. The previous
    text-mode + line-buffered approach left us starved by Windows pipe
    buffering: SteamCMD would write a line, the OS would hold it in the
    kernel buffer for minutes (especially while it was talking to Steam
    servers and producing little output), and Ctrl+C would force-flush
    everything at once.

    Reading via read1() takes whatever's available right now (down to a
    single byte), bypassing Python's text-mode line buffering. SteamCMD
    progress lines use \\r-only line breaks; we handle both \\n and \\r.

    A heartbeat thread logs '[heartbeat]' every _HEARTBEAT_QUIET_SECS of
    silence so the UI shows the subprocess hasn't actually died -- it's
    just waiting (typically on Steam network calls).
    """
    log.debug("subprocess.Popen: %s", cmd)

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    # bufsize default (-1) gives us a BufferedReader, which has read1().
    # bufsize=0 would give a raw FileIO with no read1, and is what tripped the
    # AttributeError we hit on first try. read1() on a BufferedReader returns
    # whatever's already buffered without blocking, so no real downside.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    assert proc.stdout is not None

    # Heartbeat: shared "last activity" timestamp + a worker thread.
    last_output = [time.monotonic()]  # list so the closure can mutate
    stop_heartbeat = threading.Event()

    def _heartbeat():
        while not stop_heartbeat.is_set():
            if stop_heartbeat.wait(timeout=_HEARTBEAT_QUIET_SECS):
                return  # asked to stop
            quiet = time.monotonic() - last_output[0]
            if quiet >= _HEARTBEAT_QUIET_SECS:
                _add_line(
                    f"[heartbeat] no output for {int(quiet)}s -- subprocess still"
                    f" running (PID {proc.pid}); typically waiting on Steam servers",
                    level=logging.INFO,
                )
                # Reset so we don't spam every iteration; next heartbeat
                # fires another _HEARTBEAT_QUIET_SECS later if still quiet.
                last_output[0] = time.monotonic()

    hb_thread = threading.Thread(target=_heartbeat, daemon=True,
                                 name="install-heartbeat")
    hb_thread.start()

    buf = bytearray()
    try:
        while True:
            chunk = proc.stdout.read1(4096)
            if not chunk:
                break  # EOF: process closed stdout
            last_output[0] = time.monotonic()
            buf.extend(chunk)
            # Flush every complete line in the buffer. Handle \r, \n, and \r\n.
            while True:
                i_n = buf.find(b"\n")
                i_r = buf.find(b"\r")
                if i_n == -1 and i_r == -1:
                    break
                if i_n == -1:
                    i = i_r
                elif i_r == -1:
                    i = i_n
                else:
                    i = min(i_n, i_r)
                line_bytes = bytes(buf[:i])
                # Eat the delimiter (and its \n partner if it's a \r\n pair).
                if buf[i:i + 2] == b"\r\n":
                    del buf[:i + 2]
                else:
                    del buf[:i + 1]
                if line_bytes:
                    _add_line(line_bytes.decode("utf-8", errors="replace"))
        # Drain any partial trailing line (unlikely but safe).
        if buf:
            _add_line(bytes(buf).decode("utf-8", errors="replace"))
    finally:
        stop_heartbeat.set()
        hb_thread.join(timeout=2)

    proc.wait()
    log.debug("subprocess exited rc=%d cmd=%s", proc.returncode, cmd[0])
    return proc.returncode
